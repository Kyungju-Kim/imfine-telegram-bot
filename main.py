import os
import logging
import asyncio
import calendar
from datetime import date, datetime

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from notion_helper import (
    fetch_schedule,
    fetch_my_schedule,
    format_schedule_message,
    format_my_schedule_message,
    get_target_date,
    find_notion_user_by_name,
    escape_md,
)
from user_store import register_user, get_user, list_users
from schedule_monitor import check_and_notify

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

WAITING_NAME = 1
WAITING_NAME_FROM_START = 3

MSG_ENTER_NAME = "노션에 등록된 이름을 입력해주세요 😊\n예: `홍길동`"
MSG_ERROR = (
    "⚠️ 일정을 불러오지 못했어요\\.\n\n"
    "• 잠시 후 다시 시도해주세요\n"
    "• 계속 문제가 생기면 관리자에게 문의해주세요"
)
MSG_LOADING = "⏳ 일정 불러오는 중..."

_user_tasks: dict[int, asyncio.Task] = {}


# ─── 공통: 내 일정만 조회 및 발송 (/today, /tomorrow) ────────────────

async def send_my_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, offset: int = 0):
    telegram_id = update.effective_chat.id
    user = get_user(telegram_id)

    if not user:
        await update.message.reply_text(
            "❗ 아직 등록이 안 됐어요\\!\n"
            "노션 이름으로 등록해주세요:\n"
            "`/register`",
            parse_mode="MarkdownV2",
        )
        return

    if telegram_id in _user_tasks and not _user_tasks[telegram_id].done():
        _user_tasks[telegram_id].cancel()

    async def _fetch_and_reply():
        loading_msg = await update.message.reply_text(MSG_LOADING)

        try:
            target = get_target_date(offset)
            cards = await fetch_my_schedule(target, user["notion_user_id"])
            message = format_my_schedule_message(target, cards)

            await loading_msg.edit_text(message, parse_mode="MarkdownV2")

            if offset == 0:
                from notion_helper import notion as notion_client
                from schedule_monitor import refresh_baseline

                await refresh_baseline(
                    context.application,
                    notion_client,
                    os.environ["NOTION_DATABASE_ID"],
                    str(telegram_id),
                    user,
                )

        except asyncio.CancelledError:
            try:
                await loading_msg.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error(f"[일정 조회 실패] {telegram_id}: {e}")
            try:
                await loading_msg.edit_text(MSG_ERROR, parse_mode="MarkdownV2")
            except Exception:
                pass

    task = asyncio.create_task(_fetch_and_reply())
    _user_tasks[telegram_id] = task


# ─── 공통: 전체 일정 조회 및 발송 (/date) ───────────────────────────

async def send_full_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, target: date):
    telegram_id = update.effective_chat.id
    user = get_user(telegram_id)

    if not user:
        await update.message.reply_text(
            "먼저 `/register` 로 등록해주세요\\!",
            parse_mode="MarkdownV2",
        )
        return

    loading_msg = await update.message.reply_text(MSG_LOADING)

    try:
        data = await fetch_schedule(target, user["notion_user_id"])
        message = format_schedule_message(target, data)
        await loading_msg.edit_text(message, parse_mode="MarkdownV2")

    except Exception as e:
        logger.error(f"[일정 조회 실패] {telegram_id}: {e}")
        await loading_msg.edit_text(MSG_ERROR, parse_mode="MarkdownV2")


# ─── 스케줄러: 매일 오전 8시 월~금 ───────────────────────────────────

async def _send_daily_to_user(app, telegram_id: str, user_info: dict, target: date):
    """단일 유저에게 오늘 일정 발송 (1회 재시도)"""
    from schedule_monitor import refresh_baseline
    from notion_helper import notion as notion_client

    for attempt in range(2):
        try:
            data = await fetch_schedule(target, user_info["notion_user_id"])
            message = format_schedule_message(target, data)

            await app.bot.send_message(
                chat_id=int(telegram_id),
                text=message,
                parse_mode="MarkdownV2",
            )

            logger.info(f"[스케줄러] {user_info['notion_name']} 발송 완료")

            # 발송 성공 후 baseline 갱신 (실패해도 재발송 안 함)
            try:
                await refresh_baseline(
                    app,
                    notion_client,
                    os.environ["NOTION_DATABASE_ID"],
                    telegram_id,
                    user_info,
                )
            except Exception as e:
                logger.error(f"[스케줄러] {telegram_id} baseline 갱신 실패: {e}")
                from schedule_monitor import _prev_state, _prev_state_date
                _prev_state.pop(telegram_id, None)
                _prev_state_date.pop(telegram_id, None)
            return

        except Exception as e:
            if attempt == 0:
                logger.warning(f"[스케줄러] {telegram_id} 1차 실패, 5초 후 재시도: {e}")
                await asyncio.sleep(5)
            else:
                logger.error(f"[스케줄러] {telegram_id} 최종 실패: {e}")
                retry_keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 다시 시도", callback_data=f"retry_daily_{telegram_id}")
                ]])
                try:
                    await app.bot.send_message(
                        chat_id=int(telegram_id),
                        text=(
                            "⚠️ 일정을 불러오지 못했어요\\.\n\n"
                            "아래 버튼을 눌러 다시 시도해주세요\\."
                        ),
                        parse_mode="MarkdownV2",
                        reply_markup=retry_keyboard,
                    )
                except Exception:
                    pass


async def scheduled_daily(app):
    users = list_users()

    if not users:
        logger.warning("[스케줄러] 등록된 유저 없음")
        return

    from schedule_monitor import refresh_baseline, _monitor_lock
    from notion_helper import notion as notion_client, _query_pages, _parse_schedule_from_pages

    target = get_target_date(0)

    # 전체 페이지 1번만 조회 (최대 3회 재시도)
    pages = None
    for attempt in range(3):
        try:
            pages = await _query_pages(target)
            break
        except Exception as e:
            if attempt < 2:
                logger.warning(f"[스케줄러] 페이지 조회 실패 ({attempt+1}차), 10초 후 재시도: {e}")
                await asyncio.sleep(10)
            else:
                logger.error(f"[스케줄러] 페이지 조회 최종 실패: {e}")

    async with _monitor_lock:
        for telegram_id, user_info in users.items():
            telegram_id = str(telegram_id)

            if pages is None:
                # 전체 조회 실패 시 유저별 버튼 발송
                retry_keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 다시 시도", callback_data=f"retry_daily_{telegram_id}")
                ]])
                try:
                    await app.bot.send_message(
                        chat_id=int(telegram_id),
                        text=(
                            "⚠️ 일정을 불러오지 못했어요\\.\n\n"
                            "아래 버튼을 눌러 다시 시도해주세요\\."
                        ),
                        parse_mode="MarkdownV2",
                        reply_markup=retry_keyboard,
                    )
                except Exception:
                    pass
                continue

            try:
                data = _parse_schedule_from_pages(pages, target, user_info["notion_user_id"])
                message = format_schedule_message(target, data)

                vacation = data.get("vacation", {})
                silent = (
                    user_info["notion_name"] in vacation.get("휴가", [])
                    or bool(vacation.get("공휴일", []))
                )

                await app.bot.send_message(
                    chat_id=int(telegram_id),
                    text=message,
                    parse_mode="MarkdownV2",
                    disable_notification=silent,
                )

                logger.info(f"[스케줄러] {user_info['notion_name']} 발송 완료 (silent={silent})")

                try:
                    await refresh_baseline(
                        app,
                        notion_client,
                        os.environ["NOTION_DATABASE_ID"],
                        telegram_id,
                        user_info,
                    )
                except Exception as e:
                    logger.error(f"[스케줄러] {telegram_id} baseline 갱신 실패: {e}")
                    from schedule_monitor import _prev_state, _prev_state_date
                    _prev_state.pop(telegram_id, None)
                    _prev_state_date.pop(telegram_id, None)

            except Exception as e:
                logger.error(f"[스케줄러] {telegram_id} 발송 실패: {e}")
                retry_keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 다시 시도", callback_data=f"retry_daily_{telegram_id}")
                ]])
                try:
                    await app.bot.send_message(
                        chat_id=int(telegram_id),
                        text=(
                            "⚠️ 일정을 불러오지 못했어요\\.\n\n"
                            "아래 버튼을 눌러 다시 시도해주세요\\."
                        ),
                        parse_mode="MarkdownV2",
                        reply_markup=retry_keyboard,
                    )
                except Exception:
                    pass


# ─── 버튼 콜백: 오늘 일정 재시도 ────────────────────────────────────

async def retry_daily_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    telegram_id = str(query.from_user.id)
    user = get_user(int(telegram_id))

    if not user:
        await query.edit_message_text(
            "❗ 등록된 유저를 찾을 수 없어요\\. `/register` 로 등록해주세요\\.",
            parse_mode="MarkdownV2",
        )
        return

    await query.edit_message_text(MSG_LOADING)

    try:
        from notion_helper import notion as notion_client
        from schedule_monitor import refresh_baseline

        target = get_target_date(0)
        data = await fetch_schedule(target, user["notion_user_id"])
        message = format_schedule_message(target, data)

        await query.edit_message_text(message, parse_mode="MarkdownV2")

        await refresh_baseline(
            context.application,
            notion_client,
            os.environ["NOTION_DATABASE_ID"],
            telegram_id,
            user,
        )

        logger.info(f"[재시도] {user['notion_name']} 발송 완료")

    except Exception as e:
        logger.error(f"[재시도 실패] {telegram_id}: {e}")
        retry_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 다시 시도", callback_data=f"retry_daily_{telegram_id}")
        ]])
        await query.edit_message_text(
            "⚠️ 일정을 불러오지 못했어요\\. 잠시 후 다시 시도해주세요\\.",
            parse_mode="MarkdownV2",
            reply_markup=retry_keyboard,
        )


# ─── 모니터: 3분마다 일정 변경 감지 ─────────────────────────────────

async def run_monitor(app):
    from notion_helper import notion as notion_client

    users = list_users()

    await check_and_notify(
        app,
        notion_client,
        os.environ["NOTION_DATABASE_ID"],
        users,
    )


# ─── /start ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_chat.id
    user = get_user(telegram_id)

    if not user:
        await update.message.reply_text(
            "안녕하세요\\! 전사 일정 봇이에요 👋\n\n"
            "✓ 평일 오전 8시 오늘 일정 안내\n"
            "✓ 일정 추가/변경 시 실시간 알림\n"
            "✓ 일정 시작 5분 전 미리 알림\n\n"
            f"{MSG_ENTER_NAME}",
            parse_mode="MarkdownV2",
        )
        return WAITING_NAME_FROM_START

    name = escape_md(user['notion_name'])
    await update.message.reply_text(
        f"안녕하세요\\! 전사 일정 봇이에요 👋\n"
        f"상태: ✅ 등록됨: *{name}*\n\n"
        f"✓ 평일 오전 8시 오늘 일정 안내\n"
        f"✓ 일정 추가/변경 시 실시간 알림\n"
        f"✓ 일정 시작 5분 전 미리 알림\n\n"
        f"*사용법*\n"
        f"`/left` \\- 오늘 남은 일정\n"
        f"`/today` \\- 오늘 내 일정\n"
        f"`/tomorrow` \\- 내일 내 일정\n"
        f"`/date` \\- 특정 날짜 전체 일정\n"
        f"`/register` \\- 노션 이름으로 등록",
        parse_mode="MarkdownV2",
    )

    return ConversationHandler.END


async def start_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    telegram_id = update.effective_chat.id

    await update.message.reply_text(
        f"🔍 노션에서 *{escape_md(name)}* 찾는 중\.\.\.",
        parse_mode="MarkdownV2",
    )

    notion_user, success = await find_notion_user_by_name(name)

    if not success:
        await update.message.reply_text(
            "⚠️ 노션 연결에 문제가 생겼어요\\. 잠시 후 다시 시도해주세요\\.",
            parse_mode="MarkdownV2",
        )
        return WAITING_NAME_FROM_START

    if not notion_user:
        await update.message.reply_text(
            f"❌ 노션 워크스페이스에서 *{escape_md(name)}* 을 찾을 수 없어요\\.\n\n"
            f"• 노션에 표시되는 정확한 이름인지 확인해주세요\n"
            f"• 통합\\(Integration\\)이 워크스페이스에 초대돼 있는지 확인해주세요\n\n"
            f"다시 이름을 입력해주세요 😊",
            parse_mode="MarkdownV2",
        )
        return WAITING_NAME_FROM_START

    register_user(telegram_id, notion_user["id"], notion_user["name"])

    await update.message.reply_text(
        f"✅ 등록 완료\\!\n"
        f"이름: *{escape_md(notion_user['name'])}*\n\n"
        f"오늘 일정을 바로 불러올게요\\! 🗓",
        parse_mode="MarkdownV2",
    )

    logger.info(f"[등록] {telegram_id} → {notion_user['name']} ({notion_user['id']})")

    loading_msg = await update.message.reply_text(MSG_LOADING)

    try:
        target = get_target_date(0)
        data = await fetch_schedule(target, notion_user["id"])
        message = format_schedule_message(target, data)

        await loading_msg.edit_text(message, parse_mode="MarkdownV2")

        from notion_helper import notion as notion_client
        from schedule_monitor import refresh_baseline

        user_info = {
            "notion_user_id": notion_user["id"],
            "notion_name": notion_user["name"],
        }

        await refresh_baseline(
            context.application,
            notion_client,
            os.environ["NOTION_DATABASE_ID"],
            str(telegram_id),
            user_info,
        )

    except Exception as e:
        logger.error(f"[등록 후 일정 조회 실패] {e}")
        await loading_msg.edit_text(
            "⚠️ 일정을 불러오지 못했어요\\.\n잠시 후 `/today` 로 다시 시도해주세요\\.",
            parse_mode="MarkdownV2",
        )

    return ConversationHandler.END


# ─── /register 대화 ──────────────────────────────────────────────────

async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        MSG_ENTER_NAME,
        parse_mode="MarkdownV2",
    )
    return WAITING_NAME


async def register_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    telegram_id = update.effective_chat.id

    await update.message.reply_text(
        f"🔍 노션에서 *{escape_md(name)}* 찾는 중\.\.\.",
        parse_mode="MarkdownV2",
    )

    notion_user, success = await find_notion_user_by_name(name)

    if not success:
        await update.message.reply_text(
            "⚠️ 노션 연결에 문제가 생겼어요\\. 잠시 후 다시 시도해주세요\\.",
            parse_mode="MarkdownV2",
        )
        return WAITING_NAME

    if not notion_user:
        await update.message.reply_text(
            f"❌ 노션 워크스페이스에서 *{escape_md(name)}* 을 찾을 수 없어요\\.\n\n"
            f"• 노션에 표시되는 정확한 이름인지 확인해주세요\n"
            f"• 통합\\(Integration\\)이 워크스페이스에 초대돼 있는지 확인해주세요\n\n"
            f"다시 이름을 입력해주세요 😊",
            parse_mode="MarkdownV2",
        )
        return WAITING_NAME

    register_user(telegram_id, notion_user["id"], notion_user["name"])

    await update.message.reply_text(
        f"✅ 등록 완료\\!\n"
        f"이름: *{escape_md(notion_user['name'])}*\n\n"
        f"오늘 일정을 바로 불러올게요\\! 🗓",
        parse_mode="MarkdownV2",
    )

    logger.info(f"[등록] {telegram_id} → {notion_user['name']} ({notion_user['id']})")

    loading_msg = await update.message.reply_text("⏳ 일정 불러오는 중...")

    try:
        target = get_target_date(0)
        data = await fetch_schedule(target, notion_user["id"])
        message = format_schedule_message(target, data)

        await loading_msg.edit_text(message, parse_mode="MarkdownV2")

        from notion_helper import notion as notion_client
        from schedule_monitor import refresh_baseline

        user_info = {
            "notion_user_id": notion_user["id"],
            "notion_name": notion_user["name"],
        }

        await refresh_baseline(
            context.application,
            notion_client,
            os.environ["NOTION_DATABASE_ID"],
            str(telegram_id),
            user_info,
        )

    except Exception as e:
        logger.error(f"[등록 후 일정 조회 실패] {e}")
        await loading_msg.edit_text(
            "⚠️ 일정을 불러오지 못했어요\\.\n잠시 후 `/today` 로 다시 시도해주세요\\.",
            parse_mode="MarkdownV2",
        )

    return ConversationHandler.END


# ─── /date 캘린더 ────────────────────────────────────────────────────

def build_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    today = datetime.now(KST).date()
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)

    keyboard = [
        [
            InlineKeyboardButton("◀", callback_data=f"cal_prev_{prev_year}_{prev_month:02d}"),
            InlineKeyboardButton(f"{year}년 {month}월", callback_data="cal_ignore"),
            InlineKeyboardButton("▶", callback_data=f"cal_next_{next_year}_{next_month:02d}"),
        ],
        [InlineKeyboardButton(d, callback_data="cal_ignore") for d in ["월", "화", "수", "목", "금", "토", "일"]],
    ]

    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="cal_ignore"))
            else:
                label = f"·{day}·" if date(year, month, day) == today else str(day)
                row.append(InlineKeyboardButton(label, callback_data=f"cal_date_{year}_{month:02d}_{day:02d}"))
        keyboard.append(row)

    return InlineKeyboardMarkup(keyboard)


async def cmd_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(KST)
    await update.message.reply_text(
        "📅 날짜를 선택해주세요:",
        reply_markup=build_calendar(now.year, now.month),
    )


async def calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "cal_ignore":
        return

    if data.startswith("cal_prev_") or data.startswith("cal_next_"):
        parts = data.split("_")
        year, month = int(parts[2]), int(parts[3])
        new_markup = build_calendar(year, month)
        await asyncio.gather(
            query.answer(),
            query.edit_message_reply_markup(reply_markup=new_markup),
        )
        return

    if data.startswith("cal_date_"):
        parts = data.split("_")
        year, month, day = int(parts[2]), int(parts[3]), int(parts[4])
        target = date(year, month, day)

        telegram_id = query.from_user.id
        user = get_user(telegram_id)

        if not user:
            await query.edit_message_text(
                "먼저 `/register` 로 등록해주세요\\!",
                parse_mode="MarkdownV2",
            )
            return

        await query.edit_message_text(MSG_LOADING)

        try:
            schedule_data = await fetch_schedule(target, user["notion_user_id"])
            message = format_schedule_message(target, schedule_data)
            await query.edit_message_text(message, parse_mode="MarkdownV2")
        except Exception as e:
            logger.error(f"[캘린더 일정 조회 실패] {telegram_id}: {e}")
            await query.edit_message_text(MSG_ERROR, parse_mode="MarkdownV2")


# ─── 기타 커맨드 ─────────────────────────────────────────────────────

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_my_schedule(update, context, offset=0)


async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_my_schedule(update, context, offset=1)


async def cmd_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_chat.id
    user = get_user(telegram_id)

    if not user:
        await update.message.reply_text(
            "❗ 아직 등록이 안 됐어요\\!\n`/register` 로 등록해주세요\\.",
            parse_mode="MarkdownV2",
        )
        return

    from notion_helper import notion as notion_client
    from schedule_monitor import refresh_baseline, _format_remaining_cards

    loading_msg = await update.message.reply_text(MSG_LOADING)

    try:
        current = await refresh_baseline(
            context.application,
            notion_client,
            os.environ["NOTION_DATABASE_ID"],
            str(telegram_id),
            user,
        )

        body = _format_remaining_cards(current)
        message = f"📅 *오늘 남은 일정*\n{body}"

        await loading_msg.edit_text(message, parse_mode="MarkdownV2")

    except Exception as e:
        logger.error(f"[/left 실패] {telegram_id}: {e}")
        await loading_msg.edit_text(MSG_ERROR, parse_mode="MarkdownV2")


# ─── 이름 입력 유도 핸들러 ───────────────────────────────────────────

async def _ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MSG_ENTER_NAME, parse_mode="MarkdownV2")
    return WAITING_NAME_FROM_START


async def _ask_name_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MSG_ENTER_NAME, parse_mode="MarkdownV2")
    return WAITING_NAME


# ─── 환경변수 검증 ───────────────────────────────────────────────────

def _validate_env():
    required = {
        "TELEGRAM_TOKEN": "텔레그램 봇 토큰",
        "NOTION_TOKEN": "노션 API 토큰",
        "NOTION_DATABASE_ID": "노션 데이터베이스 ID",
    }
    optional = {
        "GOOGLE_SHEETS_ID": "구글 시트 ID",
        "GOOGLE_CREDENTIALS": "구글 서비스 계정 크리덴셜",
    }
    missing = [f"{key} ({desc})" for key, desc in required.items() if not os.environ.get(key)]
    if missing:
        raise EnvironmentError(
            "필수 환경변수가 설정되지 않았어요:\n" +
            "\n".join(f"  - {m}" for m in missing)
        )
    missing_optional = [f"{key} ({desc})" for key, desc in optional.items() if not os.environ.get(key)]
    if missing_optional:
        logger.warning(
            "선택 환경변수가 없어요 (Google Sheets 백업 비활성화):\n" +
            "\n".join(f"  - {m}" for m in missing_optional)
        )


# ─── 메인 ────────────────────────────────────────────────────────────

def main():
    _validate_env()

    from user_store import restore_from_sheets
    restore_from_sheets()

    async def post_init(app):
        asyncio.create_task(run_monitor(app))

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    start_handler = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            WAITING_NAME_FROM_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, start_name_received)
            ]
        },
        fallbacks=[
            MessageHandler(filters.ALL, _ask_name)
        ],
    )

    register_handler = ConversationHandler(
        entry_points=[CommandHandler("register", cmd_register)],
        states={
            WAITING_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, register_name_received)
            ]
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.ALL, _ask_name_register),
        ],
    )

    app.add_handler(start_handler)
    app.add_handler(register_handler)

    app.add_handler(CommandHandler("date", cmd_date))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("left", cmd_left))
    app.add_handler(CallbackQueryHandler(retry_daily_callback, pattern=r"^retry_daily_"))
    app.add_handler(CallbackQueryHandler(calendar_callback, pattern=r"^cal_"))

    scheduler = AsyncIOScheduler(timezone=KST)

    scheduler.add_job(
        scheduled_daily,
        trigger="cron",
        day_of_week="mon-fri",
        hour=8,
        minute=0,
        args=[app],
        id="daily_schedule",
    )

    scheduler.add_job(
        run_monitor,
        trigger="cron",
        minute="0,3,6,9,12,15,18,21,24,27,30,33,36,39,42,45,48,51,54,57",
        args=[app],
        id="schedule_monitor",
    )

    scheduler.start()

    from schedule_monitor import set_scheduler
    set_scheduler(scheduler)

    logger.info("스케줄러 시작 (매일 오전 8시 KST, 월~금 / 3분마다 일정 모니터링)")
    logger.info("봇 시작!")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
