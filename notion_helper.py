from notion_client import AsyncClient
from datetime import datetime, date, timedelta
import os
import pytz

KST = pytz.timezone("Asia/Seoul")
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

notion = AsyncClient(auth=NOTION_TOKEN)


# ─── 날짜 유틸 ────────────────────────────────────────────────────────

def get_target_date(offset: int = 0) -> date:
    return (datetime.now(KST) + timedelta(days=offset)).date()


def format_date_korean(d: date) -> str:
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    return f"{d.month}월 {d.day}일 ({weekdays[d.weekday()]})"


def escape_md(text: str) -> str:
    """텔레그램 Markdown V1 특수문자 이스케이프"""
    if not text:
        return ""
    for char in ['_', '*', '`', '[']:
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

async def find_notion_user_by_name(name: str) -> dict | None:
    try:
        response = await notion.users.list()
        users = response.get("results", [])
        for user in users:
            if user.get("name") == name:
                return {"id": user["id"], "name": user["name"]}
        for user in users:
            if name in user.get("name", ""):
                return {"id": user["id"], "name": user["name"]}
        return None
    except Exception as e:
        print(f"[Notion 유저 검색 오류] {e}")
        return None


# ─── 공통: DB 페이지 조회 ────────────────────────────────────────────

async def _query_pages(target: date, long_range_days: int = 14) -> list:
    """Notion DB에서 target 날짜 관련 페이지 조회"""
    long_range_start = (target - timedelta(days=long_range_days)).isoformat()

    pages = []
    has_more = True
    next_cursor = None

    while has_more:
        try:
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

            response = await notion.databases.query(**kwargs)
            pages.extend(response.get("results", []))
            has_more = response.get("has_more", False)
            next_cursor = response.get("next_cursor")
        except Exception as e:
            print(f"[Notion API 오류] {e}")
            return []

    return pages


def _is_my_card(props: dict, my_notion_user_id: str) -> bool:
    check_keys = ["Assign", "cc", "담당자", "Assignee", "담당", "할당", "CC", "참조", "관련자", "사람"]
    for key in check_keys:
        if key in props:
            people = extract_people(props[key])
            if any(p["id"] == my_notion_user_id for p in people):
                return True
    return False


def _build_time_and_date(start_str, end_str):
    """start_str, end_str로부터 time_str, date_label 계산"""
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


# ─── 내 일정만 조회 (/today, /tomorrow용) ────────────────────────────

async def fetch_my_schedule(target: date, my_notion_user_id: str) -> list:
    """
    내 일정만 빠르게 조회 (휴가/출장/외근 제외)
    /today, /tomorrow 에서 사용
    """
    # 내 일정은 당일 시작 일정만 보면 되므로 long_range 최소화
    pages = await _query_pages(target, long_range_days=14)

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

        if not _is_my_card(props, my_notion_user_id):
            continue

        category = ""
        for key in ["범주", "카테고리", "Category", "category", "유형", "Type"]:
            if key in props:
                category = extract_text(props[key])
                break

        # 휴가는 내 일정에서 제외
        if "휴가" in category:
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

        my_cards.append({
            "title": title,
            "time": time_str,
            "date": date_label,
            "room": room,
            "is_trip": "출장" in category or "설치" in category or "외근" in category,
            "start_raw": start_str or "",
        })

    # start_raw 기준 정렬
    def sort_key(x):
        raw = x.get("start_raw", "")
        if not raw:
            return "9999"
        if "T" not in raw:
            return raw + "T00:00"
        return raw

    my_cards.sort(key=sort_key)
    return my_cards


# ─── 전체 일정 조회 (아침 8시, /date용) ──────────────────────────────

async def fetch_schedule(target: date, my_notion_user_id: str) -> dict:
    pages = await _query_pages(target, long_range_days=14)

    vacation_result = {"휴가": [], "오전반차": [], "오후반차": []}
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

        def get_assignees():
            for key in ["Assign", "담당자", "Assignee", "담당", "할당", "사람"]:
                if key in props:
                    return sorted([p["name"] for p in extract_people(props[key])])
            return []

        if "휴가" in category:
            assignees = get_assignees()
            if "[오전반차]" in title:
                vacation_type = "오전반차"
            elif "[오후반차]" in title:
                vacation_type = "오후반차"
            else:
                vacation_type = "휴가"
            vacation_result[vacation_type].extend(assignees)

        elif "출장" in category or "설치" in category or "외근" in category:
            assignees = get_assignees()
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
                    my_cards.append({"title": title, "time": None, "date": date_label, "room": "", "is_trip": True, "start_raw": start_str or ""})

            elif start_has_time and start_val:
                t_start = start_val.strftime("%H:%M")
                time_str = f"{t_start} ~ {end_val.strftime('%H:%M')}" if end_has_time and end_val else t_start
                outside_work.append({"names": assignees, "time": time_str, "time_raw": start_str or ""})
                if _is_my_card(props, my_notion_user_id):
                    my_cards.append({"title": title, "time": time_str, "date": None, "room": "", "is_trip": True, "start_raw": start_str or ""})

            else:
                if end_val:
                    date_label = format_short_date(start_date) if start_date == end_date else f"{format_short_date(start_date)} ~ {format_short_date(end_date)}"
                    business_trip.append({"names": assignees, "date": date_label, "start_raw": start_str or ""})
                    if _is_my_card(props, my_notion_user_id):
                        my_cards.append({"title": title, "time": None, "date": date_label, "room": "", "is_trip": True, "start_raw": start_str or ""})
                else:
                    outside_work.append({"names": assignees, "time": "종일", "time_raw": start_str or ""})
                    if _is_my_card(props, my_notion_user_id):
                        my_cards.append({"title": title, "time": "종일", "date": None, "room": "", "is_trip": True, "start_raw": start_str or ""})

        else:
            if _is_my_card(props, my_notion_user_id):
                time_str, date_label, _ = _build_time_and_date(start_str, end_str)
                room = ""
                for key in ["회의실 예약", "회의실", "장소"]:
                    if key in props:
                        room = extract_text(props[key])
                        break
                my_cards.append({"title": title, "time": time_str, "date": date_label, "room": room, "is_trip": False, "start_raw": start_str or ""})

    def my_card_sort_key(x):
        raw = x.get("start_raw", "")
        if not raw:
            return "9999"
        if "T" not in raw:
            return raw + "T00:00"
        return raw

    my_cards.sort(key=my_card_sort_key)
    vacation_result["휴가"].sort()
    vacation_result["오전반차"].sort()
    vacation_result["오후반차"].sort()
    business_trip.sort(key=lambda x: x["start_raw"])
    outside_work.sort(key=lambda x: x["time_raw"] if x["time_raw"] else "99:99")

    return {
        "vacation": vacation_result,
        "business_trip": business_trip,
        "outside_work": outside_work,
        "my_cards": my_cards
    }


# ─── 메시지 포맷 ──────────────────────────────────────────────────────

def format_my_schedule_message(target: date, cards: list) -> str:
    """내 일정만 표시 (/today, /tomorrow용)"""
    date_str = format_date_korean(target)
    lines = [f"📅 *{date_str} 내 일정*\n"]

    if cards:
        for card in cards:
            title = escape_md(card["title"] or "(제목 없음)")
            room_part = f" 📍 {escape_md(card['room'])}" if card.get("room") else ""
            if card.get("time"):
                lines.append(f"  • `{card['time']}` {title}{room_part}")
            else:
                lines.append(f"  • {title}{room_part}")
    else:
        lines.append("  • 등록된 일정이 없어요!")

    return "\n".join(lines)


def format_schedule_message(target: date, data: dict) -> str:
    """전체 일정 표시 (아침 8시, /date용)"""
    date_str = format_date_korean(target)
    lines = [f"📅 *{date_str} 일정*\n"]

    vacation = data["vacation"]
    has_vacation = any(vacation.values())

    if has_vacation:
        lines.append("🏖 *휴가/반차*")
        if vacation["휴가"]:
            lines.append(f"  • 휴가: {', '.join(escape_md(n) for n in vacation['휴가'])}")
        if vacation["오전반차"]:
            lines.append(f"  • 오전반차: {', '.join(escape_md(n) for n in vacation['오전반차'])}")
        if vacation["오후반차"]:
            lines.append(f"  • 오후반차: {', '.join(escape_md(n) for n in vacation['오후반차'])}")
        lines.append("")

    business_trip = data.get("business_trip", [])
    if business_trip:
        lines.append("✈️ *출장*")
        for item in business_trip:
            names = ", ".join(escape_md(n) for n in item["names"])
            lines.append(f"  • {item['date']} {names}")
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
            title = escape_md(card["title"] or "(제목 없음)")
            room_part = f" 📍 {escape_md(card['room'])}" if card.get("room") else ""
            if card.get("time"):
                lines.append(f"  • `{card['time']}` {title}{room_part}")
            else:
                lines.append(f"  • {title}{room_part}")
        lines.append("")
    else:
        lines.append("📌 *내 일정*")
        lines.append("  • 등록된 일정이 없어요!")
        lines.append("")

    return "\n".join(lines)
