"""
schedule_monitor.py
5분마다 오늘 내 일정을 폴링해서 추가/변경 감지 후 텔레그램 알림
"""

import logging
from datetime import datetime, date, time
import pytz

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

# 유저별 이전 카드 상태 저장
# { telegram_id: { page_id: { "edited_time": ..., "is_new": bool } } }
_prev_state: dict[str, dict] = {}


def _is_work_hour() -> bool:
    """업무시간(8시~19시) 체크"""
    now = datetime.now(KST)
    return time(8, 0) <= now.time() <= time(19, 0)


def _is_weekday() -> bool:
    """월~금 체크"""
    return datetime.now(KST).weekday() < 5


async def fetch_my_cards_today(notion_client, database_id: str, my_notion_user_id: str) -> dict:
    """
    오늘 날짜 기준 내가 assign/cc된 카드 조회
    반환: { page_id: { "title", "time", "date", "room", "created_time", "edited_time" } }
    """
    from notion_helper import (
        extract_date_range, extract_text, extract_people,
        parse_datetime_str, is_card_on_date, format_short_date
    )

    target = date.today()
    long_range_start = (target - timedelta(days=30)).isoformat()

    from datetime import timedelta
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

        # 내 카드인지 확인
        is_mine = False
        for key in check_keys:
            if key in props:
                people = [p.get("id", "") for p in props[key].get("people", [])]
                if my_notion_user_id in people:
                    is_mine = True
                    break
        if not is_mine:
            continue

        # 날짜 확인
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

        # 범주 확인 (휴가는 제외)
        category = ""
        for key in ["범주", "카테고리", "Category", "category", "유형", "Type"]:
            if key in props:
                category = extract_text(props[key])
                break
        if "휴가" in category:
            continue

        # 제목
        title = ""
        for key in ["Name", "이름", "name", "제목", "Title"]:
            if key in props:
                title = extract_text(props[key])
                break

        # 시간
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

        # 장소
        room = ""
        for key in ["회의실 예약", "회의실", "장소"]:
            if key in props:
                room = extract_text(props[key])
                break

        # 시작 시각 (정렬용)
        start_raw = start_str or ""

        result[page_id] = {
            "title": title,
            "time": time_str,
            "date": date_label,
            "room": room,
            "start_raw": start_raw,
            "created_time": page.get("created_time", ""),
            "edited_time": page.get("last_edited_time", ""),
        }

    return result


def _format_remaining_cards(cards: dict, new_ids: set, changed_ids: set) -> str:
    """현재 시각 이후 남은 카드 목록 포맷"""
    from notion_helper import escape_md

    now = datetime.now(KST)
    now_str = now.strftime("%H:%M")

    # 시간 있는 카드만 필터링 (현재 시각 이후)
    remaining = []
    no_time = []

    for page_id, card in cards.items():
        if card.get("time"):
            card_time = card["time"].split(" ~ ")[0]  # 시작 시간만
            if card_time >= now_str:
                remaining.append((page_id, card))
        else:
            no_time.append((page_id, card))

    # 시간순 정렬
    remaining.sort(key=lambda x: x[1]["start_raw"])

    lines = []
    for page_id, card in remaining + no_time:
        title = escape_md(card["title"] or "(제목 없음)")
        room_part = f"  📍 {escape_md(card['room'])}" if card.get("room") else ""

        # 새 카드 / 변경 카드 표시
        badge = ""
        if page_id in new_ids:
            badge = " 🆕"
        elif page_id in changed_ids:
            badge = " ✏️"

        if card.get("time"):
            lines.append(f"  • `{card['time']}` {title}{room_part}{badge}")
        elif card.get("date"):
            lines.append(f"  • {card['date']} {title}{badge}")
        else:
            lines.append(f"  • {title}{room_part}{badge}")

    return "\n".join(lines) if lines else "  • 남은 일정이 없어요!"


async def check_and_notify(app, notion_client, database_id: str, users: dict):
    """5분마다 호출: 변경 감지 후 알림"""
    if not _is_work_hour() or not _is_weekday():
        return

    for telegram_id, user_info in users.items():
        try:
            notion_user_id = user_info["notion_user_id"]
            current = await fetch_my_cards_today(notion_client, database_id, notion_user_id)

            prev = _prev_state.get(telegram_id, None)

            # 첫 실행이면 상태만 저장하고 알림 안 보냄
            if prev is None:
                _prev_state[telegram_id] = {
                    pid: {"edited_time": c["edited_time"]}
                    for pid, c in current.items()
                }
                continue

            # 새 카드 / 변경 카드 감지
            new_ids = set()
            changed_ids = set()

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

            # 변경사항 없으면 알림 안 보냄
            if not new_ids and not changed_ids:
                continue

            # 알림 메시지 생성
            if new_ids and not changed_ids:
                header = "🔔 *새 일정이 추가됐어요!*"
            elif changed_ids and not new_ids:
                header = "✏️ *일정이 변경됐어요!*"
            else:
                header = "🔔 *일정이 업데이트됐어요!*"

            body = _format_remaining_cards(current, new_ids, changed_ids)
            message = f"{header}\n\n📅 *오늘 남은 내 일정*\n{body}"

            await app.bot.send_message(
                chat_id=int(telegram_id),
                text=message,
                parse_mode="Markdown"
            )
            logger.info(f"[모니터] {user_info['notion_name']} 변경 알림 발송 (새:{len(new_ids)} 변경:{len(changed_ids)})")

        except Exception as e:
            logger.error(f"[모니터] {telegram_id} 처리 실패: {e}")
