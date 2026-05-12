import asyncio
from notion_client import AsyncClient
from datetime import datetime, date, timedelta
import os
import pytz

KST = pytz.timezone("Asia/Seoul")
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
LONG_RANGE_DAYS = 30
NOTION_TIMEOUT = 10  # 초

notion = AsyncClient(auth=NOTION_TOKEN)

# 휴가/공휴일 제외 카테고리 (my_cards에서 완전 제외)
EXCLUDED_CATEGORIES = {
    "휴가", "조기퇴근", "공휴일",
}

# 출장/외근 카테고리
TRIP_CATEGORIES = {
    "출장", "설치", "외근", "FineDay", "전시참관",
    "전시", "영업", "철거", "현장실사", "워크샵", "촬영", "교육",
}

# 전사 일정 카테고리
COMPANY_EVENT_CATEGORIES = {
    "세미나", "플레이샵", "신규입사", "OKR Party", "복직", "강의", "생일",
}


def _is_excluded(category: str) -> bool:
    return any(c in category for c in EXCLUDED_CATEGORIES)


def _is_trip(category: str) -> bool:
    return any(c in category for c in TRIP_CATEGORIES)


def _is_company_event(category: str) -> bool:
    return any(c in category for c in COMPANY_EVENT_CATEGORIES)


# ─── 날짜 유틸 ────────────────────────────────────────────────────────

def get_target_date(offset: int = 0) -> date:
    return (datetime.now(KST) + timedelta(days=offset)).date()


def format_date_korean(d: date) -> str:
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    return f"{d.month}월 {d.day}일 ({weekdays[d.weekday()]})"


def escape_md(text: str) -> str:
    """텔레그램 MarkdownV2 특수문자 이스케이프"""
    if not text:
        return ""
    for char in ['\\', '_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(char, f'\\{char}')
    return text


def escape_md_link_text(text: str) -> str:
    """링크 텍스트용 이스케이프"""
    if not text:
        return ""
    text = text.replace('\\', '\\\\')
    for char in ['[', ']', '(', ')', '*', '_', '`', '~']:
        text = text.replace(char, f'\\{char}')
    return text


# ─── 속성 추출 ────────────────────────────────────────────────────────

def extract_text(prop) -> str:
    if not prop:
        return ""
    ptype = prop.get("type")
    if ptype == "title":
        return "".join(t["plain_text"] for t in prop.get("title", []))
    if ptype == "rich_text":
        return "".join(t["plain_text"] for t in prop.get("rich_text", []))
    if ptype == "select":
        sel = prop.get("select")
        return sel["name"] if sel else ""
    if ptype == "multi_select":
        return ", ".join(s["name"] for s in prop.get("multi_select", []))
    return ""


def extract_people(prop) -> list[dict]:
    if not prop:
        return []
    return [
        {"id": p.get("id", ""), "name": p.get("name", "")}
        for p in prop.get("people", [])
    ]


def extract_date_range(prop) -> tuple[str | None, str | None]:
    if not prop or prop.get("type") != "date":
        return None, None
    date_obj = prop.get("date")
    if not date_obj:
        return None, None
    return date_obj.get("start"), date_obj.get("end")


def parse_datetime_str(s: str | None):
    if not s:
        return None, False
    has_time = "T" in s
    if has_time:
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = KST.localize(dt)
            return dt.astimezone(KST), True
        except Exception:
            return None, False
    else:
        try:
            return datetime.strptime(s, "%Y-%m-%d").date(), False
        except Exception:
            return None, False


def is_card_on_date(start_str, end_str, target: date) -> bool:
    start_val, start_has_time = parse_datetime_str(start_str)
    end_val, _ = parse_datetime_str(end_str)
    if start_val is None:
        return False
    start_date = start_val.date() if start_has_time else start_val
    if end_val is None:
        return start_date == target
    else:
        end_date = end_val.date() if hasattr(end_val, "date") else end_val
        return start_date <= target <= end_date


def format_short_date(d) -> str:
    if hasattr(d, "date"):
        d = d.date()
    return f"{d.month}/{d.day}"


# ─── Notion 유저 검색 ─────────────────────────────────────────────────

async def find_notion_user_by_name(name: str) -> tuple[dict | None, bool]:
    try:
        response = await notion.users.list()
        users = response.get("results", [])
        for user in users:
            if user.get("name") == name:
                return {"id": user["id"], "name": user["name"]}, True
        for user in users:
            if name in user.get("name", ""):
                return {"id": user["id"], "name": user["name"]}, True
        return None, True
    except Exception as e:
        print(f"[Notion 유저 검색 오류] {e}")
        return None, False


# ─── 공통: DB 페이지 조회 ────────────────────────────────────────────

async def _query_pages(target: date, long_range_days: int = LONG_RANGE_DAYS) -> list:
    long_range_start = (target - timedelta(days=long_range_days)).isoformat()

    pages = []
    has_more = True
    next_cursor = None

    while has_more:
        kwargs = {
            "database_id": DATABASE_ID,
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

        response = await asyncio.wait_for(
            notion.databases.query(**kwargs),
            timeout=NOTION_TIMEOUT,
        )
        pages.extend(response.get("results", []))
        has_more = response.get("has_more", False)
        next_cursor = response.get("next_cursor")

    return pages


def _is_my_card(props: dict, my_notion_user_id: str) -> bool:
    check_keys = ["Assign", "cc", "담당자", "Assignee", "담당", "할당", "CC", "참조", "관련자", "사람"]
    for key in check_keys:
        if key in props:
            people = extract_people(props[key])
            if any(p["id"] == my_notion_user_id for p in people):
                return True
    return False


def _get_assignees(props: dict) -> list[str]:
    for key in ["Assign", "담당자", "Assignee", "담당", "할당", "사람"]:
        if key in props:
            return sorted([p["name"] for p in extract_people(props[key])])
    return []


def _build_time_and_date(start_str, end_str):
    start_val, start_has_time = parse_datetime_str(start_str)
    end_val, end_has_time = parse_datetime_str(end_str)

    time_str = None
    date_label = None

    if start_has_time and start_val:
        t_start = start_val.strftime("%H:%M")
        if end_has_time and end_val:
            if start_val.date() == end_val.date():
                time_str = f"{t_start} ~ {end_val.strftime('%H:%M')}"
            else:
                time_str = (
                    f"{start_val.month}/{start_val.day} {t_start} ~ "
                    f"{end_val.month}/{end_val.day} {end_val.strftime('%H:%M')}"
                )
        else:
            time_str = t_start
    elif start_val:
        start_d = start_val.date() if hasattr(start_val, "date") else start_val
        end_d = (
            end_val.date() if hasattr(end_val, "date") else end_val
        ) if end_val else None
        if end_d and start_d != end_d:
            date_label = f"{format_short_date(start_d)} ~ {format_short_date(end_d)}"
        else:
            date_label = format_short_date(start_d)

    return time_str, date_label, start_val


def _card_title_link(card: dict) -> str:
    """제목에 노션 페이지 링크 연결 (MarkdownV2)"""
    title = escape_md_link_text(card.get("title") or "(제목 없음)")
    pid = card.get("page_id", "").replace("-", "")
    if pid:
        return f"[{title}](https://notion\\.so/{pid})"
    return escape_md(card.get("title") or "(제목 없음)")


def _sort_key(card: dict) -> str:
    raw = card.get("start_raw", "")
    if not raw:
        return "9999"
    if "T" not in raw:
        return raw + "T00:00"
    return raw


# ─── 내 카드 조회 (통합) ─────────────────────────────────────────────

async def fetch_my_cards_today(
    target: date,
    my_notion_user_id: str,
    notion_client=None,
    database_id: str = None,
) -> dict:
    client = notion_client or notion
    db_id = database_id or DATABASE_ID

    if notion_client:
        long_range_start = (target - timedelta(days=LONG_RANGE_DAYS)).isoformat()
        pages = []
        has_more = True
        next_cursor = None

        while has_more:
            kwargs = {
                "database_id": db_id,
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

            response = await asyncio.wait_for(
                client.databases.query(**kwargs),
                timeout=NOTION_TIMEOUT,
            )
            pages.extend(response.get("results", []))
            has_more = response.get("has_more", False)
            next_cursor = response.get("next_cursor")
    else:
        pages = await _query_pages(target)

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

        if _is_excluded(category):
            continue

        title = ""
        for key in ["Name", "이름", "name", "제목", "Title"]:
            if key in props:
                title = extract_text(props[key])
                break

        time_str, date_label, _ = _build_time_and_date(start_str, end_str)

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
            "end_raw": end_str or "",
            "created_time": page.get("created_time", ""),
            "edited_time": page.get("last_edited_time", ""),
            "page_id": page_id,
            "is_company_event": _is_company_event(category),  # ← 플래그 추가
        }

    return result


async def fetch_my_schedule(target: date, my_notion_user_id: str) -> list:
    cards = await fetch_my_cards_today(target, my_notion_user_id)
    return sorted(cards.values(), key=_sort_key)


# ─── 전체 일정 조회 (아침 8시, /date, 등록 직후용) ───────────────────

async def fetch_schedule(target: date, my_notion_user_id: str) -> dict:
    try:
        pages = await _query_pages(target)
    except Exception as e:
        print(f"[Notion API 오류] {e}")
        return {
            "vacation": {"휴가": [], "오전반차": [], "오후반차": [], "오전반반차": [], "오후반반차": [], "공휴일": []},
            "company_events": [],
            "business_trip": [],
            "outside_work": [],
            "my_cards": []
        }

    vacation_result = {"휴가": [], "오전반차": [], "오후반차": [], "오전반반차": [], "오후반반차": [], "공휴일": []}
    company_events = []
    business_trip = []
    outside_work = []
    my_cards = []

    for page in pages:
        props = page.get("properties", {})

        date_prop = None
        for key in ["기간", "날짜", "Date", "date", "일정"]:
            if key in props:
                date_prop = props[key]
                break
        if date_prop is None:
            continue

        start_str, end_str = extract_date_range(date_prop)
        if not is_card_on_date(start_str, end_str, target):
            continue

        title = ""
        for key in ["Name", "이름", "name", "제목", "Title"]:
            if key in props:
                title = extract_text(props[key])
                break

        category = ""
        for key in ["범주", "카테고리", "Category", "category", "유형", "Type"]:
            if key in props:
                category = extract_text(props[key])
                break

        pid = page.get("id", "")

        if "공휴일" in category:
            vacation_result["공휴일"].append(title)

        elif "휴가" in category or "조기퇴근" in category:
            assignees = _get_assignees(props)
            if "[오전반반차]" in title:
                vacation_type = "오전반반차"
            elif "[오후반반차]" in title:
                vacation_type = "오후반반차"
            elif "[오전반차]" in title:
                vacation_type = "오전반차"
            elif "[오후반차]" in title:
                vacation_type = "오후반차"
            elif "조기퇴근" in category:
                vacation_type = "오후반차"
            else:
                vacation_type = "휴가"
            vacation_result[vacation_type].extend(assignees)

        elif _is_company_event(category):
            assignees = _get_assignees(props)
            time_str, date_label, _ = _build_time_and_date(start_str, end_str)
            company_events.append({
                "title": title,
                "names": assignees,
                "time": time_str,
                "date": date_label,
                "start_raw": start_str or "",
                "page_id": pid,
            })
            if _is_my_card(props, my_notion_user_id):
                my_cards.append({
                    "title": title,
                    "time": time_str,
                    "date": date_label,
                    "room": "",
                    "is_trip": False,
                    "is_company_event": True,
                    "start_raw": start_str or "",
                    "page_id": pid,
                })

        elif _is_trip(category):
            assignees = _get_assignees(props)
            start_val, start_has_time = parse_datetime_str(start_str)
            end_val, end_has_time = parse_datetime_str(end_str)

            start_date = start_val.date() if hasattr(start_val, "date") else start_val
            end_date = (
                end_val.date() if hasattr(end_val, "date") else end_val
                if end_val else start_date
            )

            if end_date and start_date != end_date:
                date_label = f"{format_short_date(start_date)} ~ {format_short_date(end_date)}"
                business_trip.append({"names": assignees, "date": date_label, "start_raw": start_str or ""})
                if _is_my_card(props, my_notion_user_id):
                    my_cards.append({"title": title, "time": None, "date": date_label, "room": "", "is_trip": True, "start_raw": start_str or "", "page_id": pid})

            elif start_has_time and start_val:
                t_start = start_val.strftime("%H:%M")
                time_str = f"{t_start} ~ {end_val.strftime('%H:%M')}" if end_has_time and end_val else t_start
                outside_work.append({"names": assignees, "time": time_str, "time_raw": start_str or ""})
                if _is_my_card(props, my_notion_user_id):
                    my_cards.append({"title": title, "time": time_str, "date": None, "room": "", "is_trip": True, "start_raw": start_str or "", "page_id": pid})

            else:
                if end_val:
                    date_label = format_short_date(start_date) if start_date == end_date else f"{format_short_date(start_date)} ~ {format_short_date(end_date)}"
                    business_trip.append({"names": assignees, "date": date_label, "start_raw": start_str or ""})
                    if _is_my_card(props, my_notion_user_id):
                        my_cards.append({"title": title, "time": None, "date": date_label, "room": "", "is_trip": True, "start_raw": start_str or "", "page_id": pid})
                else:
                    outside_work.append({"names": assignees, "time": "종일", "time_raw": start_str or ""})
                    if _is_my_card(props, my_notion_user_id):
                        my_cards.append({"title": title, "time": None, "date": None, "room": "", "is_trip": True, "start_raw": start_str or "", "page_id": pid})

        else:
            if _is_my_card(props, my_notion_user_id):
                time_str, date_label, _ = _build_time_and_date(start_str, end_str)
                room = ""
                for key in ["회의실 예약", "회의실", "장소"]:
                    if key in props:
                        room = extract_text(props[key])
                        break
                my_cards.append({"title": title, "time": time_str, "date": date_label, "room": room, "is_trip": False, "start_raw": start_str or "", "page_id": pid})

    my_cards.sort(key=_sort_key)
    vacation_result["휴가"].sort()
    vacation_result["오전반차"].sort()
    vacation_result["오후반차"].sort()
    vacation_result["오전반반차"].sort()
    vacation_result["오후반반차"].sort()
    vacation_result["공휴일"].sort()
    company_events.sort(key=lambda x: x["start_raw"])
    business_trip.sort(key=lambda x: x["start_raw"])
    outside_work.sort(key=lambda x: "00:00" if not x["time_raw"] else x["time_raw"])

    return {
        "vacation": vacation_result,
        "company_events": company_events,
        "business_trip": business_trip,
        "outside_work": outside_work,
        "my_cards": my_cards
    }


# ─── 메시지 포맷 ──────────────────────────────────────────────────────

def _fmt_card_line(card: dict) -> str:
    title = _card_title_link(card)
    room_part = f" 📍 {escape_md(card['room'])}" if card.get("room") else ""
    if card.get("time"):
        return f"  • `{card['time']}` {title}{room_part}"
    return f"  • {title}{room_part}"


def _fmt_company_event_line(card: dict) -> str:
    """전사 일정 한 줄 포맷 (시간/날짜 범위 prefix)"""
    title = _card_title_link(card)
    if card.get("time"):
        prefix = f"`{card['time']}` "
    elif card.get("date") and "~" in card["date"]:
        prefix = f"{escape_md(card['date'])} "
    else:
        prefix = ""
    return f"  • {prefix}{title}"


def format_my_schedule_message(target: date, cards: list) -> str:
    date_str = escape_md(format_date_korean(target))
    lines = [f"📅 *{date_str} 내 일정*"]

    if cards:
        for card in cards:
            lines.append(_fmt_card_line(card))
    else:
        lines.append("  • 등록된 일정이 없어요\\!")

    return "\n".join(lines)


def format_schedule_message(target: date, data: dict) -> str:
    date_str = escape_md(format_date_korean(target))
    lines = [f"📅 *{date_str} 일정*\n"]

    company_events = data.get("company_events", [])
    if company_events:
        lines.append("🌟 *전사 일정*")
        for item in company_events:
            title = _card_title_link(item)
            if item.get("time"):
                prefix = f"`{item['time']}` "
            elif item.get("date") and "~" in item["date"]:
                prefix = f"{escape_md(item['date'])} "
            else:
                prefix = ""
            if item["names"]:
                names = ", ".join(escape_md(n) for n in item["names"])
                lines.append(f"  • {prefix}{title} {names}")
            else:
                lines.append(f"  • {prefix}{title}")
        lines.append("")

    vacation = data["vacation"]
    has_vacation = any(vacation.values())

    if has_vacation:
        lines.append("🏖 *휴가/반차*")
        if vacation["공휴일"]:
            lines.append(f"  • 공휴일: {', '.join(escape_md(n) for n in vacation['공휴일'])}")
        if vacation["휴가"]:
            lines.append(f"  • 휴가: {', '.join(escape_md(n) for n in vacation['휴가'])}")
        if vacation["오전반반차"]:
            lines.append(f"  • 오전반반차: {', '.join(escape_md(n) for n in vacation['오전반반차'])}")
        if vacation["오전반차"]:
            lines.append(f"  • 오전반차: {', '.join(escape_md(n) for n in vacation['오전반차'])}")
        if vacation["오후반차"]:
            lines.append(f"  • 오후반차: {', '.join(escape_md(n) for n in vacation['오후반차'])}")
        if vacation["오후반반차"]:
            lines.append(f"  • 오후반반차: {', '.join(escape_md(n) for n in vacation['오후반반차'])}")
        lines.append("")

    business_trip = data.get("business_trip", [])
    if business_trip:
        lines.append("✈️ *출장*")
        for item in business_trip:
            names = ", ".join(escape_md(n) for n in item["names"])
            lines.append(f"  • {escape_md(item['date'])} {names}")
        lines.append("")

    outside_work = data.get("outside_work", [])
    if outside_work:
        lines.append("🚗 *외근*")
        for item in outside_work:
            names = ", ".join(escape_md(n) for n in item["names"])
            lines.append(f"  • `{item['time']}` {names}")
        lines.append("")

    my_cards = data["my_cards"]
    if my_cards:
        lines.append("📌 *내 일정*")
        for card in my_cards:
            lines.append(_fmt_card_line(card))
        lines.append("")
    else:
        lines.append("📌 *내 일정*")
        lines.append("  • 등록된 일정이 없어요\\!")
        lines.append("")

    return "\n".join(lines)
