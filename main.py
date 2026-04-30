import os
import logging
from datetime import date

import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
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
# 스케줄러 자동 발송 대상 Chat ID들 (콤마 구분)
# 예: "123456,789012"
BROADCAST_CHAT_IDS = os.environ.get("BROADCAST_CHAT_IDS", "").split(",")


# ─── 공통: 일정 조회 및 발송 ─────────────────────────────────────────

async def send_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, offset: int = 0):
    telegram_id = update.effective_chat.id
    user = get_user(telegram_id)

    if not user:
        await update.message.reply_text(
            "❗ 아직 등록이 안 됐어!\n"
            "노션 이름으로 등록해줘:\n"
            "`/register 홍길동`",
            parse_mode="Markdown"
        )
        return

    target = get_target_date(offset)
    data = await fetch_schedule(target, user["notion_user_id"])
    message = format_schedule_message(target, data)
    await update.message.reply_text(message, parse_mode="Markdown")


# ─── 스케줄러: 매일 오전 8시 등록된 전원에게 발송 ─────────────────────

async def scheduled_daily(app):
    users = list_users()
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
            logger.error(f"[스케줄러] {telegram_id} 발송 실패: {e}")


# ─── 커맨드 핸들러 ───────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_chat.id
    user = get_user(telegram_id)

    if not user:
        await update.message.reply_text(
            f"안녕! 전사 일정 봇이야 👋\n\n"
            f"노션 이름으로 등록하면 바로 사용할 수 있어!\n"
            f"`/register 홍길동`",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        f"안녕! 전사 일정 봇이야 👋\n"
        f"상태: ✅ 등록됨: *{user['notion_name']}*\n\n"
        f"*사용법*\n"
        f"`/register 홍길동` - 노션 이름으로 등록\n"
        f"`/unregister` - 등록 해제\n"
        f"`/today` - 오늘 일정\n"
        f"`/tomorrow` - 내일 일정\n"
        f"`/dayafter` - 모레 일정\n"
        f"`/in3days` - 3일 후 일정\n"
        f"`/yesterday` - 어제 일정\n"
        f"`/date 2024-01-15` - 특정 날짜 일정",
        parse_mode="Markdown"
    )


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /register 홍길동
    노션에서 해당 이름 유저를 찾아서 매핑 저장
    """
    if not context.args:
        await update.message.reply_text(
            "노션에 등록된 이름을 입력해줘!\n예: `/register 홍길동`",
            parse_mode="Markdown"
        )
        return

    name = " ".join(context.args)
    telegram_id = update.effective_chat.id

    await update.message.reply_text(f"🔍 노션에서 *{name}* 찾는 중...", parse_mode="Markdown")

    notion_user = await find_notion_user_by_name(name)

    if not notion_user:
        await update.message.reply_text(
            f"❌ 노션 워크스페이스에서 *{name}* 을 찾을 수 없어.\n\n"
            f"• 노션에 표시되는 정확한 이름인지 확인해봐\n"
            f"• 통합(Integration)이 워크스페이스에 초대돼 있는지 확인해봐",
            parse_mode="Markdown"
        )
        return

    register_user(telegram_id, notion_user["id"], notion_user["name"])
    await update.message.reply_text(
        f"✅ 등록 완료!\n"
        f"이름: *{notion_user['name']}*\n\n"
        f"이제 `/today` 로 일정 확인해봐!",
        parse_mode="Markdown"
    )
    logger.info(f"[등록] {telegram_id} → {notion_user['name']} ({notion_user['id']})")


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

async def cmd_specific(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("날짜를 입력해줘! 예: `/date 2024-01-15`", parse_mode="Markdown")
        return
    try:
        d = date.fromisoformat(context.args[0])
        telegram_id = update.effective_chat.id
        user = get_user(telegram_id)
        if not user:
            await update.message.reply_text("먼저 `/register 이름` 으로 등록해줘!", parse_mode="Markdown")
            return
        data = await fetch_schedule(d, user["notion_user_id"])
        message = format_schedule_message(d, data)
        await update.message.reply_text(message, parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("`YYYY-MM-DD` 형식으로 입력해줘!", parse_mode="Markdown")


# ─── 메인 ────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("unregister", cmd_unregister))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("dayafter", cmd_day_after))
    app.add_handler(CommandHandler("in3days", cmd_3days))
    app.add_handler(CommandHandler("yesterday", cmd_yesterday))
    app.add_handler(CommandHandler("date", cmd_specific))

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
