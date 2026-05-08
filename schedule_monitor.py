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
    """
    5분 전 알림 job 등록.

    중요:
    기존 job을 먼저 제거한 뒤 새로 등록한다.
    그래야 일정 시간이 바뀌거나, 시간 있는 일정이 시간 없는 일정으로 바뀌거나,
    이미 지난 시간으로 바뀐 경우에도 예전 알림이 남지 않는다.
    """
    if not _scheduler:
        return

    telegram_id = str(telegram_id)
    job_id = f"reminder_{telegram_id}_{page_id}"

    # 기존 job 먼저 제거
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
        logger.info(f"[5분 전 알림 기존 job 제거] {telegram_id} - {page_id}")

    # 시간이 없는 일정은 5분 전 알림 등록하지 않음
    if not card.get("time"):
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

    _scheduler.add_job(
        _send_reminder,
        trigger="date",
        run_date=notify_at,
        args=[app, telegram_id, card],
        id=job_id,
        timezone=KST
    )

    logger.info(
        f"[5분 전 알림 등록] {telegram_id} - {card['title']} at {notify_at.strftime('%H:%M')}"
    )


def _remove_reminder(telegram_id: str, page_id: str):
    """5분 전 알림 job 제거"""
    if not _scheduler:
        return

    telegram_id = str(telegram_id)
    job_id = f"reminder_{telegram_id}_{page_id}"

    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
        logger.info(f"[5분 전 알림 제거] {telegram_id} - {page_id}")


def register_all_reminders(app, telegram_id: str, cards: dict):
    """오늘 카드 전체에 대해 5분 전 알림 등록"""
    telegram_id = str(telegram_id)

    for page_id, card in cards.items():
        _register_reminder(app, telegram_id, page_id, card)


# ─── Notion 카드 조회 ─────────────────────────────────────────────────

async def fetch_my_cards_today(notion_client, database_id: str, my_notion_user_id: str) -> dict:
    from notion_helper import (
        extract_date_range,
        extract_text,
        extract_people,
        parse_datetime_str,
        is_card_on_date,
        format_short_date,
    )

    # 서버 로컬 시간이 아니라 한국 시간 기준 오늘
    target = datetime.now(KST).date()
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
                                {"property": "기간", "date": {"on_or_before": target.isoformat()}},
                            ]
                        },
                        {
                            "and": [
                                {"property": "기간", "date": {"on_or_after": long_range_start}},
                                {"property": "기간", "date": {"on_or_before": target.isoformat()}},
                            ]
                        },
                    ]
                },
                "page_size": 100,
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

    check_keys = [
        "Assign",
        "cc",
        "담당자",
        "Assignee",
        "담당",
        "할당",
        "CC",
        "참조",
        "관련자",
        "사람",
    ]

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

        # 휴가는 내 일정 변경/5분 전 알림 대상에서 제외
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

            if start_d == end_d:
                date_label = format_short_date(start_d)
            else:
                date_label = f"{format_short_date(start_d)} ~ {format_short_date(end_d)}"

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


# ─── 기준 상태 갱신 ─────────────────────────────────────────────────

def _make_state(cards: dict) -> dict:
    """변경 감지를 위한 최소 상태만 저장"""
    return {
        pid: {"edited_time": c["edited_time"]}
        for pid, c in cards.items()
    }


async def refresh_baseline(app, notion_client, database_id: str, telegram_id: str, user_info: dict) -> dict:
    """
    현재 오늘 내 일정을 변경 감지 기준 상태로 저장한다.

    이 함수는 다음 상황에서 호출한다.
    - 매일 오전 8시 오늘 일정 발송 직후
    - /today 로 오늘 일정 확인 직후
    - /update 로 오늘 남은 일정 확인 직후
    - /start 또는 /register 후 오늘 일정 조회 직후

    동시에 5분 전 알림도 현재 일정 기준으로 완전히 재정렬한다.
    """
    telegram_id = str(telegram_id)

    current = await fetch_my_cards_today(
        notion_client,
        database_id,
        user_info["notion_user_id"]
    )

    prev = _prev_state.get(telegram_id, {})

    # 기존에 알고 있던 일정의 5분 전 알림 job 제거
    for page_id in prev.keys():
        _remove_reminder(telegram_id, page_id)

    # 현재 상태를 기준 상태로 저장
    _prev_state[telegram_id] = _make_state(current)

    # 현재 일정 기준으로 5분 전 알림 다시 등록
    register_all_reminders(app, telegram_id, current)

    logger.info(
        f"[기준 상태 갱신] {user_info.get('notion_name', telegram_id)} - {len(current)}건"
    )

    return current


# ─── 폴링: 변경 감지 + 알림 ──────────────────────────────────────────

async def check_and_notify(app, notion_client, database_id: str, users: dict):
    if not _is_work_hour() or not _is_weekday():
        return

    for telegram_id, user_info in users.items():
        telegram_id = str(telegram_id)

        try:
            notion_user_id = user_info["notion_user_id"]

            current = await fetch_my_cards_today(
                notion_client,
                database_id,
                notion_user_id
            )

            prev = _prev_state.get(telegram_id, None)

            # 첫 실행: 상태 저장 + 5분 전 알림 등록
            # 이 시점에는 비교 기준이 없으므로 변경 알림은 보내지 않음
            if prev is None:
                _prev_state[telegram_id] = _make_state(current)
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
            _prev_state[telegram_id] = _make_state(current)

            # 5분 전 알림 갱신
            # 새 일정: 새로 등록
            for page_id in new_ids:
                _register_reminder(app, telegram_id, page_id, current[page_id])

            # 변경 일정: 기존 job 제거 후 현재 일정 기준으로 재등록
            for page_id in changed_ids:
                _register_reminder(app, telegram_id, page_id, current[page_id])

            # 삭제 일정: 기존 job 제거
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
            message = f"{header}\n\n📅 *오늘 남은 일정*\n{body}"

            await app.bot.send_message(
                chat_id=int(telegram_id),
                text=message,
                parse_mode="Markdown"
            )

            logger.info(
                f"[모니터] {user_info['notion_name']} 변경 알림 발송 "
                f"(새:{len(new_ids)} 변경:{len(changed_ids)} 삭제:{len(deleted_ids)})"
            )

        except Exception as e:
            logger.error(f"[모니터] {telegram_id} 처리 실패: {e}")


async def force_check(app, notion_client, database_id: str, telegram_id: str, user_info: dict):
    try:
        telegram_id = str(telegram_id)

        current = await refresh_baseline(
            app,
            notion_client,
            database_id,
            telegram_id,
            user_info
        )

        body = _format_remaining_cards(current)
        message = f"📅 *오늘 남은 일정*\n{body}"

        await app.bot.send_message(
            chat_id=int(telegram_id),
            text=message,
            parse_mode="Markdown"
        )

        logger.info(f"[강제 업데이트] {user_info['notion_name']} 완료")

    except Exception as e:
        logger.error(f"[강제 업데이트] {telegram_id}: {e}")
        raise
