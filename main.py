import os
import logging
from datetime import date

import pytz
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from notion_helper import (
    fetch_schedule, format_schedule_message,
    get_target_date, find_notion_user_by_name
)
from user_store import register_user, get_user, remove_user, list_users

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# ConversationHandler 상태
WAITING_NAME = 1
WAITING_DATE = 2


# ─── 공통: 일정 조회 및 발송 ─────────────────────────────────────────

async def send_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, offset: int = 0):
    telegram_id = update.effective_chat.id
    user = get_user(telegram_id)

    if not user:
        await update.message.reply_text(
            "❗ 아직 등록이 안 됐어!\n"
            "노션 이름으로 등록해줘:\n"
            "`/register`",
            parse_mode="Markdown"
        )
        return

    target = get_target_date(offset)
    data = await fetch_schedule(target, user["notion_user_id"])
    message = format_schedule_message(target, data)
    await update.message.reply_text(message, parse_mode="Markdown")


# ─── 스케줄러: 매일 오전 8시 ─────────────────────────────────────────

async def scheduled_daily(app):
    users = list_users()
    if not users:
        logger.warning("[스케줄러] 등록된 유저 없음")
        return

    target = get_target_date(0)
    for telegram_id, user_info in users.items():
        try:
            data = await fetch_schedule(target, user_info["notion_user_id"])
            message = format_schedule_message(target, data)
            await app.bot.send_message(
                chat_id=int(telegram_id),
                text=message,
                parse_mode="Markdown"
            )
            logger.info(f"[스케줄러] {user_info['notion_name']} 발송 완료")
        except Exception as e:
            try:
                await app.bot.send_message(
                    chat_id=int(telegram_id),
                    text="⚠️ 일정 발송 중 오류가 발생했어!\n`/start` 로 상태 확인해줘.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            logger.error(f"[스케줄러] {telegram_id} 발송 실패: {e}")


# ─── /start ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_chat.id
    user = get_user(telegram_id)

    if not user:
        await update.message.reply_text(
            f"안녕! 전사 일정 봇이야 👋\n\n"
            f"매일 오전 8시에 오늘 일정을 자동으로 알려줄게!\n\n"
            f"노션 이름으로 등록하면 바로 사용할 수 있어!\n"
            f"`/register`",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        f"안녕! 전사 일정 봇이야 👋\n"
        f"상태: ✅ 등록됨: *{user['notion_name']}*\n\n"
        f"📢 매일 오전 8시에 오늘 일정을 자동으로 알려줄게!\n\n"
        f"*사용법*\n"
        f"`/register` - 노션 이름으로 등록\n"
        f"`/unregister` - 등록 해제\n"
        f"`/today` - 오늘 일정\n"
        f"`/tomorrow` - 내일 일정\n"
        f"`/dayafter` - 모레 일정\n"
        f"`/in3days` - 3일 후 일정\n"
        f"`/yesterday` - 어제 일정\n"
        f"`/date` - 특정 날짜 일정",
        parse_mode="Markdown"
    )


# ─── /register 대화 ──────────────────────────────────────────────────

async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "노션에 등록된 이름을 입력해줘!\n예: `홍길동`",
        parse_mode="Markdown"
    )
    return WAITING_NAME


async def register_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    telegram_id = update.effective_chat.id

    await update.message.reply_text(f"🔍 노션에서 *{name}* 찾는 중...", parse_mode="Markdown")

    notion_user = await find_notion_user_by_name(name)

    if not notion_user:
        await update.message.reply_text(
            f"❌ 노션 워크스페이스에서 *{name}* 을 찾을 수 없어.\n\n"
            f"• 노션에 표시되는 정확한 이름인지 확인해봐\n"
            f"• 통합(Integration)이 워크스페이스에 초대돼 있는지 확인해봐\n\n"
            f"다시 시도하려면 `/register`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    register_user(telegram_id, notion_user["id"], notion_user["name"])
    await update.message.reply_text(
        f"✅ 등록 완료!\n"
        f"이름: *{notion_user['name']}*\n\n"
        f"이제 `/today` 로 일정 확인해봐!",
        parse_mode="Markdown"
    )
    logger.info(f"[등록] {telegram_id} → {notion_user['name']} ({notion_user['id']})")
    return ConversationHandler.END


# ─── /date 대화 ──────────────────────────────────────────────────────

async def cmd_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "조회할 날짜를 입력해줘!\n예: `2024-01-15`",
        parse_mode="Markdown"
    )
    return WAITING_DATE


async def date_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_chat.id
    user = get_user(telegram_id)

    if not user:
        await update.message.reply_text("먼저 `/register` 로 등록해줘!", parse_mode="Markdown")
        return ConversationHandler.END

    try:
        d = date.fromisoformat(update.message.text.strip())
        data = await fetch_schedule(d, user["notion_user_id"])
        message = format_schedule_message(d, data)
        await update.message.reply_text(message, parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text(
            "`YYYY-MM-DD` 형식으로 입력해줘!\n예: `2024-01-15`\n\n다시 시도하려면 `/date`",
            parse_mode="Markdown"
        )
    return ConversationHandler.END


# ─── 대화 취소 ───────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("취소했어!")
    return ConversationHandler.END


# ─── 기타 커맨드 ─────────────────────────────────────────────────────

async def cmd_unregister(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_chat.id
    user = get_user(telegram_id)
    if not user:
        await update.message.reply_text("등록된 정보가 없어!")
        return
    remove_user(telegram_id)
    await update.message.reply_text(f"✅ *{user['notion_name']}* 등록 해제 완료!", parse_mode="Markdown")

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_schedule(update, context, offset=0)

async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_schedule(update, context, offset=1)

async def cmd_day_after(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_schedule(update, context, offset=2)

async def cmd_3days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_schedule(update, context, offset=3)

async def cmd_yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_schedule(update, context, offset=-1)


# ─── 메인 ────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # /register 대화 핸들러
    register_handler = ConversationHandler(
        entry_points=[CommandHandler("register", cmd_register)],
        states={
            WAITING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name_received)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    # /date 대화 핸들러
    date_handler = ConversationHandler(
        entry_points=[CommandHandler("date", cmd_date)],
        states={
            WAITING_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, date_received)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(register_handler)
    app.add_handler(date_handler)
    app.add_handler(CommandHandler("unregister", cmd_unregister))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("dayafter", cmd_day_after))
    app.add_handler(CommandHandler("in3days", cmd_3days))
    app.add_handler(CommandHandler("yesterday", cmd_yesterday))

    scheduler = AsyncIOScheduler(timezone=KST)
    scheduler.add_job(
        scheduled_daily,
        trigger="cron",
        hour=8,
        minute=0,
        args=[app],
        id="daily_schedule"
    )
    scheduler.start()
    logger.info("스케줄러 시작 (매일 오전 8시 KST)")

    logger.info("봇 시작!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
