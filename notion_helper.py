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


# ─── Notion 유저 검색 ─────────────────────────────────────────────────

async def find_notion_user_by_name(name: str) -> dict | None:
    """
    워크스페이스 유저 목록에서 이름으로 검색
    반환: {"id": "...", "name": "..."} 또는 None
    """
    try:
        response = await notion.users.list()
        users = response.get("results", [])
        # 정확히 일치하는 이름 우선
        for user in users:
            if user.get("name") == name:
                return {"id": user["id"], "name": user["name"]}
        # 없으면 포함 검색
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
    start_range = (target - timedelta(days=7)).isoformat()
    end_range = (target + timedelta(days=1)).isoformat()

    try:
        response = await notion.databases.query(
            database_id=DATABASE_ID,
            filter={
            "and": [
                {
                    "property": "기간",  # ← 기간으로 변경
                    "date": {"on_or_after": start_range}
                },
                {
                    "property": "기간",  # ← 기간으로 변경
                    "date": {"on_or_before": end_range}
                }
            ]
        },
            page_size=100
        )
    except Exception as e:
        print(f"[Notion API 오류] {e}")
        return {"vacation": {"휴가": [], "오전반차": [], "오후반차": []}, "my_cards": []}

    pages = response.get("results", [])
    vacation_result = {"휴가": [], "오전반차": [], "오후반차": []}
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

        # 휴가 카드
        if "휴가" in category:
            assignees = []
            for key in ["Assign", "담당자", "Assignee", "담당", "할당", "사람"]:
                if key in props:
                    assignees = [p["name"] for p in extract_people(props[key])]
                    break
            # 카드 이름으로 반차 구분
            if "[오전반차]" in title:
                vacation_type = "오전반차"
            elif "[오후반차]" in title:
                vacation_type = "오후반차"
            else:
                vacation_type = "휴가"
            vacation_result[vacation_type].extend(assignees)

        # 내 카드
        if _is_my_card(props, my_notion_user_id):
            time_str = None
            start_val, start_has_time = parse_datetime_str(start_str)
            end_val, end_has_time = parse_datetime_str(end_str)
            if start_has_time and start_val:
                t_start = start_val.strftime("%H:%M")
                if end_has_time and end_val:
                    time_str = f"{t_start} ~ {end_val.strftime('%H:%M')}"
                else:
                    time_str = t_start

            # 회의실 예약 추출
            room = ""
            for key in ["회의실 예약", "회의실", "장소"]:
                if key in props:
                    room = extract_text(props[key])
                    break

            my_cards.append({
                "title": title,
                "time": time_str,
                "room": room
            })

    # 시간순 정렬 (시간 없는 카드는 맨 뒤로)
    my_cards.sort(key=lambda x: x["time"] if x["time"] else "99:99")

    return {"vacation": vacation_result, "my_cards": my_cards}


# ─── 메시지 포맷 ──────────────────────────────────────────────────────

def format_schedule_message(target: date, data: dict) -> str:
    date_str = format_date_korean(target)
    lines = [f"📅 *{date_str} 일정*\n"]

    vacation = data["vacation"]
    has_vacation = any(vacation.values())

    if has_vacation:
        lines.append("🏖 *휴가/반차*")
        if vacation["휴가"]:
            lines.append(f"  • 휴가: {', '.join(vacation['휴가'])}")
        if vacation["오전반차"]:
            lines.append(f"  • 오전반차: {', '.join(vacation['오전반차'])}")
        if vacation["오후반차"]:
            lines.append(f"  • 오후반차: {', '.join(vacation['오후반차'])}")
        lines.append("")

    my_cards = data["my_cards"]
    if my_cards:
        lines.append("📌 *내 일정*")
        for card in my_cards:
            title = card["title"] or "(제목 없음)"
            room_part = f" [{card['room']}]" if card.get("room") else ""
            if card["time"]:
                lines.append(f"  • `{card['time']}` {title}{room_part}")
            else:
                lines.append(f"  • {title}{room_part}")
        lines.append("")

    if not has_vacation and not my_cards:
        lines.append("✅ 등록된 일정이 없어요!")

    return "\n".join(lines)
