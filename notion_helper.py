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


# ─── 일정 조회 ────────────────────────────────────────────────────────

def _is_my_card(props: dict, my_notion_user_id: str) -> bool:
    check_keys = ["Assign", "cc", "담당자", "Assignee", "담당", "할당", "CC", "참조", "관련자", "사람"]
    for key in check_keys:
        if key in props:
            people = extract_people(props[key])
            if any(p["id"] == my_notion_user_id for p in people):
                return True
    return False


async def fetch_schedule(target: date, my_notion_user_id: str) -> dict:
    long_range_start = (target - timedelta(days=60)).isoformat()

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
            return {
                "vacation": {"휴가": [], "오전반차": [], "오후반차": []},
                "business_trip": [],
                "outside_work": [],
                "my_cards": []
            }

    vacation_result = {"휴가": [], "오전반차": [], "오후반차": []}
    business_trip = []  # 출장: 날짜 기간
    outside_work = []   # 외근: 시간
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

        # 휴가 카드
        if "휴가" in category:
            assignees = get_assignees()
            if "[오전반차]" in title:
                vacation_type = "오전반차"
            elif "[오후반차]" in title:
                vacation_type = "오후반차"
            else:
                vacation_type = "휴가"
            vacation_result[vacation_type].extend(assignees)

        # 출장/설치/외근 카드
        elif "출장" in category or "설치" in category or "외근" in category:
            assignees = get_assignees()
            start_val, start_has_time = parse_datetime_str(start_str)
            end_val, end_has_time = parse_datetime_str(end_str)

            if start_has_time and start_val:
                # 시간 있으면 → 외근
                t_start = start_val.strftime("%H:%M")
                if end_has_time and end_val:
                    time_str = f"{t_start} ~ {end_val.strftime('%H:%M')}"
                else:
                    time_str = t_start
                outside_work.append({
                    "names": assignees,
                    "time": time_str,
                    "time_raw": start_str or ""
                })
                # 내가 포함된 외근이면 내 일정에도 추가
                if _is_my_card(props, my_notion_user_id):
                    my_cards.append({
                        "title": title,
                        "time": time_str,
                        "date": None,
                        "room": "",
                        "is_trip": True
                    })
            else:
                # 날짜 기간 → 출장
                start_d = start_val.date() if hasattr(start_val, "date") else start_val
                end_d = end_val.date() if hasattr(end_val, "date") else end_val if end_val else start_d
                if start_d == end_d:
                    date_label = format_short_date(start_d)
                else:
                    date_label = f"{format_short_date(start_d)} ~ {format_short_date(end_d)}"
                business_trip.append({
                    "names": assignees,
                    "date": date_label,
                    "start_raw": start_str or ""
                })
                # 내가 포함된 출장이면 내 일정에도 추가
                if _is_my_card(props, my_notion_user_id):
                    my_cards.append({
                        "title": title,
                        "time": None,
                        "date": date_label,
                        "room": "",
                        "is_trip": True
                    })

        # 일반 내 카드 (휴가/출장/설치/외근 제외)
        if "휴가" not in category and "출장" not in category and "설치" not in category and "외근" not in category:
            if _is_my_card(props, my_notion_user_id):
                start_val, start_has_time = parse_datetime_str(start_str)
                end_val, end_has_time = parse_datetime_str(end_str)
                time_str = None
                if start_has_time and start_val:
                    t_start = start_val.strftime("%H:%M")
                    if end_has_time and end_val:
                        time_str = f"{t_start} ~ {end_val.strftime('%H:%M')}"
                    else:
                        time_str = t_start

                room = ""
                for key in ["회의실 예약", "회의실", "장소"]:
                    if key in props:
                        room = extract_text(props[key])
                        break

                my_cards.append({
                    "title": title,
                    "time": time_str,
                    "date": None,
                    "room": room,
                    "is_trip": False
                })

    # 정렬
    def my_card_sort_key(x):
        if x.get("time"):
            return x["time"]
        if x.get("date"):
            return "00:00"  # 날짜만 있는 출장은 맨 앞
        return "99:99"

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

def format_schedule_message(target: date, data: dict) -> str:
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

    # 출장 (날짜 기간)
    business_trip = data.get("business_trip", [])
    if business_trip:
        lines.append("✈️ *출장*")
        for item in business_trip:
            names = ", ".join(escape_md(n) for n in item["names"])
            lines.append(f"  • {item['date']} {names}")
        lines.append("")

    # 외근 (시간)
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
            room_part = f"  📍 {escape_md(card['room'])}" if card.get("room") else ""
            if card.get("time"):
                lines.append(f"  • `{card['time']}` {title}{room_part}")
            elif card.get("date"):
                lines.append(f"  • {card['date']} {title}")
            else:
                lines.append(f"  • {title}{room_part}")
        lines.append("")
    else:
        lines.append("📌 *내 일정*")
        lines.append("  • 등록된 일정이 없어요!")
        lines.append("")

    return "\n".join(lines)
