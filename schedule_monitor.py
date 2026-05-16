"""
schedule_monitor.py
3분마다 오늘 내 일정을 폴링해서 추가/변경 감지 후 텔레그램 알림
+ 시간이 있는 일정 5분 전 알림
"""

import asyncio
import logging
import os
from datetime import datetime, time, timedelta, date
from zoneinfo import ZoneInfo

from notion_helper import fetch_my_cards_today, get_target_date

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

DEBUG_MODE = (
    os.getenv("DEBUG_MODE", "false").lower() == "true"
)

_prev_state: dict[str, dict] = {}
_prev_state_date: dict[str, date] = {}
_scheduler = None
_monitor_lock = asyncio.Lock()


def set_scheduler(scheduler):
    global _scheduler
    _scheduler = scheduler


def _is_work_hour() -> bool:
    if DEBUG_MODE:
        return True
    now = datetime.now(KST)
    return time(8, 0) <= now.time() <= time(19, 0)


def _is_weekday() -> bool:
    if DEBUG_MODE:
        return True
    return datetime.now(KST).weekday() < 5


# ─── 5분 전 알림 ─────────────────────────────────────────────────────

async def _send_reminder(
    app,
    notion_client,
    database_id: str,
    telegram_id: str,
    notion_user_id: str,
    page_id: str,
):
    from notion_helper import escape_md, parse_datetime_str, _card_title_link

    try:
        target = get_target_date(0)
        result = await fetch_my_cards_today(
            target,
            notion_user_id,
            notion_client=notion_client,
            database_id=database_id,
        )
        current = result

        if page_id not in current:
            logger.info(f"[리마인더 skip] 삭제된 일정 {page_id}")
            return

        card = current[page_id]

        if not card.get("time"):
            logger.info(f"[리마인더 skip] 시간 없는 일정 {page_id}")
            return

        start_val, start_has_time = parse_datetime_str(card["start_raw"])

        if not start_has_time or not start_val:
            return

        now = datetime.now(KST)
        diff = (start_val - now).total_seconds()

        if diff <= 0 or diff > 360:
            logger.info(f"[리마인더 skip] 유효하지 않은 reminder (remaining={diff:.0f}s)")
            return

        title = _card_title_link(card)
        room_part = f" 📍 {escape_md(card['room'])}" if card.get("room") else ""

        message = (
            f"⏰ *잠시 후 일정이 있어요\\!*\n"
            f"  • `{card['time']}` {title}{room_part}"
        )

        for attempt in range(2):
            try:
                await app.bot.send_message(
                    chat_id=int(telegram_id),
                    text=message,
                    parse_mode="MarkdownV2"
                )
                logger.info(f"[5분 전 알림] {telegram_id} - {card['title']}")
                break
            except Exception as e:
                if attempt == 0:
                    logger.warning(f"[5분 전 알림 재시도] {telegram_id}: {e}")
                    await asyncio.sleep(2)
                else:
                    logger.error(f"[5분 전 알림 실패] {telegram_id}: {e}")

    except Exception as e:
        logger.error(f"[5분 전 알림 실패] {telegram_id}: {e}")


def _register_reminder(
    app,
    notion_client,
    database_id: str,
    telegram_id: str,
    notion_user_id: str,
    page_id: str,
    card: dict,
):
    if not _scheduler:
        return

    telegram_id = str(telegram_id)
    job_id = f"reminder_{telegram_id}_{page_id}"

    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
        logger.info(f"[5분 전 알림 기존 job 제거] {telegram_id} - {page_id}")

    if not card.get("time"):
        return

    from notion_helper import parse_datetime_str

    start_val, start_has_time = parse_datetime_str(card["start_raw"])

    if not start_has_time or not start_val:
        return

    notify_at = start_val - timedelta(minutes=5)
    now = datetime.now(KST)

    if notify_at <= now:
        if now < start_val:
            logger.info(f"[늦은 리마인더 즉시 발송] {telegram_id} - {card['title']}")
            _scheduler.add_job(
                _send_reminder,
                trigger="date",
                run_date=now + timedelta(seconds=1),
                args=[app, notion_client, database_id, telegram_id, notion_user_id, page_id],
                id=job_id,
                replace_existing=True,
                timezone=KST
            )
        return

    _scheduler.add_job(
        _send_reminder,
        trigger="date",
        run_date=notify_at,
        args=[app, notion_client, database_id, telegram_id, notion_user_id, page_id],
        id=job_id,
        replace_existing=True,
        timezone=KST
    )

    logger.info(f"[5분 전 알림 등록] {telegram_id} - {card['title']} at {notify_at.strftime('%H:%M')}")


def _remove_reminder(telegram_id: str, page_id: str):
    if not _scheduler:
        return

    telegram_id = str(telegram_id)
    job_id = f"reminder_{telegram_id}_{page_id}"

    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
        logger.info(f"[5분 전 알림 제거] {telegram_id} - {page_id}")


def register_all_reminders(app, notion_client, database_id, telegram_id, notion_user_id, cards):
    telegram_id = str(telegram_id)
    for page_id, card in cards.items():
        _register_reminder(app, notion_client, database_id, telegram_id, notion_user_id, page_id, card)


# ─── 메시지 포맷 ─────────────────────────────────────────────────────

def _format_remaining_cards(cards: dict) -> str:
    from notion_helper import escape_md, parse_datetime_str, _card_title_link

    now = datetime.now(KST)
    remaining = []
    no_time = []

    for page_id, card in cards.items():
        if card.get("time"):
            start_val, _ = parse_datetime_str(card["start_raw"])
            end_val, _ = parse_datetime_str(card.get("end_raw"))

            if end_val:
                if end_val >= now:
                    remaining.append((page_id, card))
            else:
                if start_val and start_val >= now:
                    remaining.append((page_id, card))
        else:
            no_time.append((page_id, card))

    remaining.sort(key=lambda x: x[1].get("start_raw") or "")

    lines = []
    for page_id, card in remaining + no_time:
        title = _card_title_link(card)
        room_part = f" 📍 {escape_md(card['room'])}" if card.get("room") else ""

        if card.get("time"):
            lines.append(f"  • `{card['time']}` {title}{room_part}")
        else:
            lines.append(f"  • {title}{room_part}")

    return "\n".join(lines) if lines else "  • 남은 일정이 없어요\\!"


# ─── 기준 상태 갱신 ─────────────────────────────────────────────────

def _make_state(cards: dict) -> dict:
    return {
        pid: {
            "edited_time": c["edited_time"],
            "title": c["title"],
        }
        for pid, c in cards.items()
    }


async def refresh_baseline(app, notion_client, database_id: str, telegram_id: str, user_info: dict) -> dict:
    telegram_id = str(telegram_id)
    target = get_target_date(0)

    result = await fetch_my_cards_today(
        target,
        user_info["notion_user_id"],
        notion_client=notion_client,
        database_id=database_id,
    )
    current = result

    prev = _prev_state.get(telegram_id, {})

    deleted_ids = set(prev.keys()) - set(current.keys())
    for page_id in deleted_ids:
        _remove_reminder(telegram_id, page_id)

    for page_id, card in current.items():
        if (
            page_id not in prev
            or prev[page_id]["edited_time"] != card["edited_time"]
        ):
            _register_reminder(
                app, notion_client, database_id,
                telegram_id, user_info["notion_user_id"], page_id, card,
            )

    _prev_state[telegram_id] = _make_state(current)
    _prev_state_date[telegram_id] = target

    logger.info(f"[기준 상태 갱신] {user_info.get('notion_name', telegram_id)} - {len(current)}건")

    return current


# ─── 폴링: 변경 감지 + 알림 ──────────────────────────────────────────

async def check_and_notify(app, notion_client, database_id: str, users: dict):
    if not _is_work_hour() or not _is_weekday():
        return

    if _monitor_lock.locked():
        logger.warning("[모니터] 이전 폴링 실행 중, 스킵")
        return

    async with _monitor_lock:
        from notion_helper import _query_pages, _filter_my_cards_from_pages, get_target_date

        target = get_target_date(0)

        # 전체 페이지 1번만 조회 (오늘치만, 2회 재시도)
        pages = None
        for attempt in range(2):
            try:
                pages = await _query_pages(target, long_range_days=0)
                break
            except Exception as e:
                if attempt < 1:
                    logger.warning(f"[모니터] 페이지 조회 실패, 5초 후 재시도: {e}")
                    await asyncio.sleep(5)
                else:
                    logger.error(f"[모니터] 페이지 조회 최종 실패, 이번 폴링 스킵: {e}")

        if pages is None:
            return

        for telegram_id, user_info in users.items():
            telegram_id = str(telegram_id)

            try:
                notion_user_id = user_info["notion_user_id"]

                current = _filter_my_cards_from_pages(pages, target, notion_user_id)

                prev = _prev_state.get(telegram_id, None)
                today = target

                # 날짜가 바뀌었으면 prev_state 초기화
                if prev is not None and _prev_state_date.get(telegram_id) != today:
                    logger.info(f"[모니터] {telegram_id} 날짜 변경 감지 → 상태 초기화")
                    prev = None
                    _prev_state.pop(telegram_id, None)
                    _prev_state_date.pop(telegram_id, None)

                if prev is None:
                    _prev_state[telegram_id] = _make_state(current)
                    _prev_state_date[telegram_id] = today
                    register_all_reminders(app, notion_client, database_id, telegram_id, notion_user_id, current)
                    continue

                new_ids = list()
                changed_ids = list()
                deleted_ids = list(set(prev.keys()) - set(current.keys()))

                for page_id, card in current.items():
                    if page_id not in prev:
                        new_ids.append(page_id)
                    elif prev[page_id]["edited_time"] != card["edited_time"]:
                        changed_ids.append(page_id)

                for page_id in new_ids:
                    _register_reminder(app, notion_client, database_id, telegram_id, notion_user_id, page_id, current[page_id])
                for page_id in changed_ids:
                    _register_reminder(app, notion_client, database_id, telegram_id, notion_user_id, page_id, current[page_id])
                for page_id in deleted_ids:
                    _remove_reminder(telegram_id, page_id)

                if not new_ids and not changed_ids and not deleted_ids:
                    _prev_state[telegram_id] = _make_state(current)
                    _prev_state_date[telegram_id] = today
                    continue

                from notion_helper import escape_md

                if new_ids and not changed_ids and not deleted_ids:
                    header = "🔔 *새 일정이 추가됐어요\\!*"
                elif changed_ids and not new_ids and not deleted_ids:
                    header = "🔔 *일정이 변경됐어요\\!*"
                elif deleted_ids and not new_ids and not changed_ids:
                    header = "🔔 *일정이 삭제됐어요\\!*"
                else:
                    header = "🔔 *일정이 업데이트됐어요\\!*"

                detail_lines = []
                for pid in new_ids:
                    t = escape_md(current[pid]["title"] or "(제목 없음)")
                    detail_lines.append(f"  • {t}")
                for pid in changed_ids:
                    t = escape_md(current[pid]["title"] or "(제목 없음)")
                    detail_lines.append(f"  • {t}")
                for pid in deleted_ids:
                    t = escape_md(prev[pid].get("title", "(제목 없음)"))
                    detail_lines.append(f"  • ~{t}~")

                detail = "\n".join(detail_lines)
                body = _format_remaining_cards(current)
                message = f"{header}\n{detail}\n\n📅 *오늘 남은 일정*\n{body}"

                await app.bot.send_message(
                    chat_id=int(telegram_id),
                    text=message,
                    parse_mode="MarkdownV2"
                )

                # 발송 성공 후 상태 갱신
                _prev_state[telegram_id] = _make_state(current)
                _prev_state_date[telegram_id] = today

                logger.info(
                    f"[모니터] {user_info['notion_name']} 변경 알림 발송 "
                    f"(새:{len(new_ids)} 변경:{len(changed_ids)} 삭제:{len(deleted_ids)})"
                )

            except Exception as e:
                logger.error(f"[모니터] {telegram_id} 처리 실패: {type(e).__name__}: {e}")
