import os
import logging
import asyncio
from datetime import datetime

import pytz
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from notion_helper import fetch_schedule, format_schedule_message, get_target_date

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


# ─── 공통: 날짜 offset으로 일정 메시지 전송 ───────────────────────────
async def send_schedule(context: ContextTypes.DEFAULT_TYPE | None, chat_id: str, offset: int = 0):
    target = get_target_date(offset)
    data = await fetch_schedule(target)
    message = format_schedule_message(target, data)
    if context:
        await context.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="Markdown"
        )
    return message


# ─── 스케줄러: 매일 오전 8시 자동 발송 ──────────────────────────────
async def scheduled_daily(app):
    target = get_target_date(0)
    data = await fetch_schedule(target)
    message = format_schedule_message(target, data)
    await app.bot.send_message(
        chat_id=CHAT_ID,
        text=message,
        parse_mode="Markdown"
    )
    logger.info(f"[스케줄러] 오전 8시 일정 발송 완료: {target}")


# ─── 커맨드 핸들러 ──────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"✅ 봇이 정상 작동 중이야!\n"
        f"Chat ID: `{chat_id}`\n\n"
        f"사용 가능한 명령어:\n"
        f"/오늘 - 오늘 일정\n"
        f"/내일 - 내일 일정\n"
        f"/모레 - 모레 일정\n"
        f"/글피 - 3일 후 일정\n"
        f"/어제 - 어제 일정",
        parse_mode="Markdown"
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_schedule(context, update.effective_chat.id, offset=0)


async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_schedule(context, update.effective_chat.id, offset=1)


async def cmd_day_after_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_schedule(context, update.effective_chat.id, offset=2)


async def cmd_3days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_schedule(context, update.effective_chat.id, offset=3)


async def cmd_yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_schedule(context, update.effective_chat.id, offset=-1)


# 날짜 직접 입력: /일정 2024-01-15
async def cmd_specific(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("날짜를 입력해줘! 예: `/일정 2024-01-15`", parse_mode="Markdown")
        return
    try:
        from datetime import date
        d = date.fromisoformat(context.args[0])
        data = await fetch_schedule(d)
        message = format_schedule_message(d, data)
        await update.message.reply_text(message, parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("날짜 형식이 올바르지 않아. `YYYY-MM-DD` 형식으로 입력해줘!", parse_mode="Markdown")


# ─── 메인 ────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # 명령어 등록
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("오늘", cmd_today))
    app.add_handler(CommandHandler("내일", cmd_tomorrow))
    app.add_handler(CommandHandler("모레", cmd_day_after_tomorrow))
    app.add_handler(CommandHandler("글피", cmd_3days))
    app.add_handler(CommandHandler("어제", cmd_yesterday))
    app.add_handler(CommandHandler("일정", cmd_specific))

    # APScheduler 설정
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
    logger.info("스케줄러 시작 완료 (매일 오전 8시 KST)")

    logger.info("텔레그램 봇 시작...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
