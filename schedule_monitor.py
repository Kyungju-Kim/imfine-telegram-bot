"""
schedule_monitor.py
3분마다 오늘 내 일정을 폴링해서 추가/변경 감지 후 텔레그램 알림
+ 시간이 있는 일정 5분 전 알림
"""

import logging
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

_prev_state: dict[str, dict] = {}

# 스케줄러 참조 (main.py에서 주입)
_scheduler = None

def set_scheduler(scheduler):
    global _scheduler
    _scheduler = scheduler


def _is_work_hour() -> bool:
    now = datetime.now(KST)
    return time(8, 0) <= now.time() <= time(19, 0)


def _is_weekday() -> bool:
    return datetime.now(KST).weekday() < 5


# ─── 5분 전 알림 ─────────────────────────────────────────────────────

async def _send_reminder(app, telegram_id: str, card: dict):
    """5분 전 알림 발송"""
    from notion_helper import escape_md
    title = escape_md(card["title"] or "(제목 없음)")
    room_part = f" 📍 {escape_md(card['room'])}" if card.get("room") else ""
    time_part = f"`{card['time']}`" if card.get("time") else ""

    message = (
        f"⏰ *5분 후 일정이 있어요!*\n"
        f"  • {time_part} {title}{room_part}"
    )
    try:
        await app.bot.send_message(
            chat_id=int(telegram_id),
            text=message,
            parse_mode="Markdown"
        )
        logger.info(f"[5분 전 알림] {telegram_id} - {card['title']}")
    except Exception as e:
        logger.error(f"[5분 전 알림 실패] {telegram_id}: {e}")


def _register_reminder(app, telegram_id: str, page_id: str, card: dict):
    """5분 전 알림 job 등록"""
    if not _scheduler or not card.get("time"):
        return

    from notion_helper import parse_datetime_str
    start_val, start_has_time = parse_datetime_str(card["start_raw"])
    if not start_has_time or not start_val:
        return

    notify_at = start_val - timedelta(minutes=5)
    now = datetime.now(KST)

    # 이미 지난 시각이면 등록 안 함
    if notify_at <= now:
        return

    job_id = f"reminder_{telegram_id}_{page_id}"

    # 기존 job 있으면 제거
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)

    _scheduler.add_job(
        _send_reminder,
        trigger="date",
        run_date=notify_at,
        args=[app, telegram_id, card],
        id=job_id,
        timezone=KST
    )
    logger.info(f"[5분 전 알림 등록] {telegram_id} - {card['title']} at {notify_at.strftime('%H:%M')}")


def _remove_reminder(telegram_id: str, page_id: str):
    """5분 전 알림 job 제거"""
    if not _scheduler:
        return
    job_id = f"reminder_{telegram_id}_{page_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
        logger.info(f"[5분 전 알림 제거] {telegram_id} - {page_id}")


def register_all_reminders(app, telegram_id: str, cards: dict):
    """오늘 카드 전체에 대해 5분 전 알림 등록"""
    for page_id, card in cards.items():
        _register_reminder(app, telegram_id, page_id, card)


# ─── Notion 카드 조회 ─────────────────────────────────────────────────

async def fetch_my_cards_today(notion_client, database_id: str, my_notion_user_id: str) -> dict:
    from notion_helper import (
        extract_date_range, extract_text, extract_people,
        parse_datetime_str, is_card_on_date, format_short_date
    )

    target = date.today()
    long_range_start = (target - timedelta(days=30)).isoformat()

    pages = []
    has_more = True
    next_cursor = None

    while has_more:
        try:
            kwargs = {
                "database_id": database_id,
                "filter": {
                    "or": [
                        {
                            "and": [
                                {"property": "기간", "date": {"on_or_after": target.isoformat()}},
                                {"property": "기간", "date": {"on_or_before": target.isoformat()}}
                            ]
                        },
                        {
                            "and": [
                                {"property": "기간", "date": {"on_or_after": long_range_start}},
                                {"property": "기간", "date": {"on_or_before": target.isoformat()}}
                            ]
                        }
                    ]
                },
                "page_size": 100
            }
            if next_cursor:
                kwargs["start_cursor"] = next_cursor

            response = await notion_client.databases.query(**kwargs)
            pages.extend(response.get("results", []))
            has_more = response.get("has_more", False)
            next_cursor = response.get("next_cursor")
        except Exception as e:
            logger.error(f"[모니터] Notion API 오류: {e}")
            return {}

    result = {}
    check_keys = ["Assign", "cc", "담당자", "Assignee", "담당", "할당", "CC", "참조", "관련자", "사람"]

    for page in pages:
        props = page.get("properties", {})
        page_id = page["id"]

        is_mine = False
        for key in check_keys:
            if key in props:
                people = [p.get("id", "") for p in props[key].get("people", [])]
                if my_notion_user_id in people:
                    is_mine = True
                    break
        if not is_mine:
            continue

        date_prop = None
        for key in ["기간", "날짜", "Date", "date", "일정"]:
            if key in props:
                date_prop = props[key]
                break
        if not date_prop:
            continue

        start_str, end_str = extract_date_range(date_prop)
        if not is_card_on_date(start_str, end_str, target):
            continue

        category = ""
        for key in ["범주", "카테고리", "Category", "category", "유형", "Type"]:
            if key in props:
                category = extract_text(props[key])
                break
        if "휴가" in category:
            continue

        title = ""
        for key in ["Name", "이름", "name", "제목", "Title"]:
            if key in props:
                title = extract_text(props[key])
                break

        start_val, start_has_time = parse_datetime_str(start_str)
        end_val, end_has_time = parse_datetime_str(end_str)
        time_str = None
        date_label = None

        if start_has_time and start_val:
            t_start = start_val.strftime("%H:%M")
            if end_has_time and end_val:
                time_str = f"{t_start} ~ {end_val.strftime('%H:%M')}"
            else:
                time_str = t_start
        elif start_val and end_val:
            start_d = start_val.date() if hasattr(start_val, "date") else start_val
            end_d = end_val.date() if hasattr(end_val, "date") else end_val
            date_label = format_short_date(start_d) if start_d == end_d else f"{format_short_date(start_d)} ~ {format_short_date(end_d)}"

        room = ""
        for key in ["회의실 예약", "회의실", "장소"]:
            if key in props:
                room = extract_text(props[key])
                break

        result[page_id] = {
            "title": title,
            "time": time_str,
            "date": date_label,
            "room": room,
            "start_raw": start_str or "",
            "created_time": page.get("created_time", ""),
            "edited_time": page.get("last_edited_time", ""),
        }

    return result


# ─── 메시지 포맷 ─────────────────────────────────────────────────────

def _format_remaining_cards(cards: dict) -> str:
    from notion_helper import escape_md

    now = datetime.now(KST)
    now_str = now.strftime("%H:%M")

    remaining = []
    no_time = []

    for page_id, card in cards.items():
        if card.get("time"):
            card_time = card["time"].split(" ~ ")[0]
            if card_time >= now_str:
                remaining.append((page_id, card))
        else:
            no_time.append((page_id, card))

    remaining.sort(key=lambda x: x[1]["start_raw"])

    lines = []
    for page_id, card in remaining + no_time:
        title = escape_md(card["title"] or "(제목 없음)")
        room_part = f" 📍 {escape_md(card['room'])}" if card.get("room") else ""

        if card.get("time"):
            lines.append(f"  • `{card['time']}` {title}{room_part}")
        elif card.get("date"):
            lines.append(f"  • {card['date']} {title}")
        else:
            lines.append(f"  • {title}{room_part}")

    return "\n".join(lines) if lines else "  • 남은 일정이 없어요!"


# ─── 폴링: 변경 감지 + 알림 ──────────────────────────────────────────

async def check_and_notify(app, notion_client, database_id: str, users: dict):
    if not _is_work_hour() or not _is_weekday():
        return

    for telegram_id, user_info in users.items():
        try:
            notion_user_id = user_info["notion_user_id"]
            current = await fetch_my_cards_today(notion_client, database_id, notion_user_id)

            prev = _prev_state.get(telegram_id, None)

            # 첫 실행: 상태 저장 + 5분 전 알림 등록
            if prev is None:
                _prev_state[telegram_id] = {
                    pid: {"edited_time": c["edited_time"]}
                    for pid, c in current.items()
                }
                register_all_reminders(app, telegram_id, current)
                continue

            new_ids = set()
            changed_ids = set()
            deleted_ids = set(prev.keys()) - set(current.keys())

            for page_id, card in current.items():
                if page_id not in prev:
                    new_ids.add(page_id)
                elif prev[page_id]["edited_time"] != card["edited_time"]:
                    changed_ids.add(page_id)

            # 상태 업데이트
            _prev_state[telegram_id] = {
                pid: {"edited_time": c["edited_time"]}
                for pid, c in current.items()
            }

            # 5분 전 알림 갱신
            for page_id in new_ids:
                _register_reminder(app, telegram_id, page_id, current[page_id])
            for page_id in changed_ids:
                _register_reminder(app, telegram_id, page_id, current[page_id])
            for page_id in deleted_ids:
                _remove_reminder(telegram_id, page_id)

            if not new_ids and not changed_ids:
                continue

            if new_ids and not changed_ids:
                header = "🔔 *새 일정이 추가됐어요!*"
            elif changed_ids and not new_ids:
                header = "🔔 *일정이 변경됐어요!*"
            else:
                header = "🔔 *일정이 업데이트됐어요!*"

            body = _format_remaining_cards(current)
            message = f"{header}\n\n📅 *오늘 남은 일정*\n\n{body}"

            await app.bot.send_message(
                chat_id=int(telegram_id),
                text=message,
                parse_mode="Markdown"
            )
            logger.info(f"[모니터] {user_info['notion_name']} 변경 알림 발송 (새:{len(new_ids)} 변경:{len(changed_ids)} 삭제:{len(deleted_ids)})")

        except Exception as e:
            logger.error(f"[모니터] {telegram_id} 처리 실패: {e}")


async def force_check(app, notion_client, database_id: str, telegram_id: str, user_info: dict):
    try:
        current = await fetch_my_cards_today(notion_client, database_id, user_info["notion_user_id"])

        _prev_state[telegram_id] = {
            pid: {"edited_time": c["edited_time"]}
            for pid, c in current.items()
        }

        # 5분 전 알림도 갱신
        register_all_reminders(app, telegram_id, current)

        body = _format_remaining_cards(current)
        message = f"📅 *오늘 남은 일정*\n\n{body}"

        await app.bot.send_message(
            chat_id=int(telegram_id),
            text=message,
            parse_mode="Markdown"
        )
        logger.info(f"[강제 업데이트] {user_info['notion_name']} 완료")
    except Exception as e:
        logger.error(f"[강제 업데이트] {telegram_id}: {e}")
        raise
