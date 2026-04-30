from notion_client import AsyncClient
from datetime import datetime, date, timedelta
import os
import pytz

KST = pytz.timezone("Asia/Seoul")
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
MY_NOTION_USER_ID = os.environ["MY_NOTION_USER_ID"]

notion = AsyncClient(auth=NOTION_TOKEN)


def get_target_date(offset: int = 0) -> date:
    return (datetime.now(KST) + timedelta(days=offset)).date()


def format_date_korean(d: date) -> str:
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    return f"{d.month}월 {d.day}일 ({weekdays[d.weekday()]})"


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


def extract_people(prop) -> list[str]:
    if not prop:
        return []
    return [p.get("name", p.get("id", "")) for p in prop.get("people", [])]


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
        # 시간 포함: "2024-01-15T09:00:00.000+09:00"
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
    """카드가 target 날짜에 해당하는지 확인"""
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


def is_my_card(props: dict) -> bool:
    """내가 assignee 또는 cc인 카드인지 확인"""
    # 노션 DB의 실제 속성명에 맞게 조정 필요
    assignee_candidates = ["담당자", "Assignee", "assign", "담당", "할당"]
    cc_candidates = ["CC", "cc", "참조", "관련자"]

    for key in assignee_candidates:
        if key in props:
            people = extract_people(props[key])
            if MY_NOTION_USER_ID in [p for p in props[key].get("people", []) if p.get("id") == MY_NOTION_USER_ID]:
                return True
            # 이름으로도 체크 (fallback)
            my_name = os.environ.get("MY_NOTION_NAME", "")
            if my_name and my_name in people:
                return True

    for key in cc_candidates:
        if key in props:
            my_name = os.environ.get("MY_NOTION_NAME", "")
            if my_name:
                people = extract_people(props[key])
                if my_name in people:
                    return True
            # ID로 체크
            for p in props[key].get("people", []):
                if p.get("id") == MY_NOTION_USER_ID:
                    return True

    return False


async def fetch_schedule(target: date) -> dict:
    """
    반환 형태:
    {
        "vacation": {"휴가": [...이름], "오전반차": [...이름], "오후반차": [...이름]},
        "my_cards": [{"title": ..., "time": ..., "category": ...}]
    }
    """
    # 날짜 필터: target 날짜를 포함하는 카드
    # Notion API는 date 필터로 정확한 범위 쿼리가 제한적이므로 ±7일 범위로 가져와서 Python에서 필터링
    start_range = (target - timedelta(days=7)).isoformat()
    end_range = (target + timedelta(days=1)).isoformat()

    try:
        response = await notion.databases.query(
            database_id=DATABASE_ID,
            filter={
                "and": [
                    {
                        "property": "날짜",  # ← 실제 날짜 속성명으로 교체
                        "date": {
                            "on_or_after": start_range
                        }
                    },
                    {
                        "property": "날짜",  # ← 실제 날짜 속성명으로 교체
                        "date": {
                            "on_or_before": end_range
                        }
                    }
                ]
            },
            page_size=100
        )
    except Exception as e:
        print(f"[Notion API Error] {e}")
        return {"vacation": {"휴가": [], "오전반차": [], "오후반차": []}, "my_cards": []}

    pages = response.get("results", [])

    vacation_result = {"휴가": [], "오전반차": [], "오후반차": []}
    my_cards = []

    for page in pages:
        props = page.get("properties", {})

        # 날짜 속성명 탐색 (여러 후보)
        date_prop = None
        for key in ["날짜", "Date", "date", "일정", "기간"]:
            if key in props:
                date_prop = props[key]
                break

        if date_prop is None:
            continue

        start_str, end_str = extract_date_range(date_prop)
        if not is_card_on_date(start_str, end_str, target):
            continue

        # 제목
        title = ""
        for key in ["이름", "Name", "name", "제목", "Title"]:
            if key in props:
                title = extract_text(props[key])
                break

        # 범주/카테고리
        category = ""
        for key in ["범주", "카테고리", "Category", "category", "유형", "Type"]:
            if key in props:
                category = extract_text(props[key])
                break

        # 휴가 카드 처리
        if "휴가" in category:
            assignees = []
            for key in ["담당자", "Assignee", "assign", "담당", "할당", "사람"]:
                if key in props:
                    assignees = extract_people(props[key])
                    break

            vacation_type = title if title in ["휴가", "오전반차", "오후반차"] else "휴가"
            if vacation_type in vacation_result:
                vacation_result[vacation_type].extend(assignees)
            else:
                vacation_result["휴가"].extend(assignees)

        # 내 카드 처리
        if is_my_card(props):
            # 시간 추출
            time_str = None
            start_val, start_has_time = parse_datetime_str(start_str)
            end_val, end_has_time = parse_datetime_str(end_str)

            if start_has_time and start_val:
                t_start = start_val.strftime("%H:%M")
                if end_has_time and end_val:
                    t_end = end_val.strftime("%H:%M")
                    time_str = f"{t_start} ~ {t_end}"
                else:
                    time_str = t_start

            my_cards.append({
                "title": title,
                "time": time_str,
                "category": category
            })

    return {"vacation": vacation_result, "my_cards": my_cards}


def format_schedule_message(target: date, data: dict) -> str:
    date_str = format_date_korean(target)
    lines = [f"📅 *{date_str} 일정*\n"]

    # 휴가 섹션
    vacation = data["vacation"]
    has_vacation = any(vacation.values())

    if has_vacation:
        lines.append("🏖 *휴가/반차*")
        if vacation["오전반차"]:
            lines.append(f"  🌅 오전반차: {', '.join(vacation['오전반차'])}")
        if vacation["오후반차"]:
            lines.append(f"  🌇 오후반차: {', '.join(vacation['오후반차'])}")
        if vacation["휴가"]:
            lines.append(f"  🏝 휴가: {', '.join(vacation['휴가'])}")
        lines.append("")

    # 내 일정 섹션
    my_cards = data["my_cards"]
    if my_cards:
        lines.append("📌 *내 일정*")
        for card in my_cards:
            title = card["title"] or "(제목 없음)"
            time_part = f" `{card['time']}`" if card["time"] else ""
            category_part = f" _{card['category']}_" if card["category"] else ""
            lines.append(f"  • {title}{time_part}{category_part}")
        lines.append("")

    if not has_vacation and not my_cards:
        lines.append("✅ 오늘은 등록된 일정이 없어요!")

    return "\n".join(lines)
