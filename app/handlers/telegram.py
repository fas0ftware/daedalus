"""Telegram wiring: one Agent per chat, owner-only access, rate limit, chunked replies."""

from __future__ import annotations

import asyncio
import logging

from ..agent import Agent
from ..sandbox import run_python, workspace_for
from ..security import RateLimiter, clean_input, is_allowed

log = logging.getLogger("daedalus")
TELEGRAM_LIMIT = 4000

HELP = ("Send me a coding task or question. I can run Python in an isolated sandbox to check things.\n"
        "/reset  start a fresh conversation\n/help   this text")


def chunks(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return parts or [""]


def build_app(settings, client):
    from telegram import Update
    from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

    agents: dict[int, Agent] = {}
    # One request at a time per chat: two messages sent quickly would otherwise run the
    # same Agent in two threads and interleave their turns in its history.
    locks: dict[int, asyncio.Lock] = {}
    limiter = RateLimiter(settings.requests_per_hour)

    def agent_for(chat_id: int) -> Agent:
        if chat_id not in agents:
            workdir = workspace_for(settings.workspace, chat_id)
            agents[chat_id] = Agent(
                client, settings.model, effort=settings.effort, max_steps=settings.max_steps,
                run_code=lambda code: run_python(code, settings.sandbox_image, workdir, settings.sandbox_timeout))
        return agents[chat_id]

    async def guard(update: Update) -> bool:
        user = update.effective_user
        if not is_allowed(user.id if user else None, settings.allowed_user_ids):
            log.warning("refused user %s", user.id if user else "?")
            return False
        return True

    def lock_for(chat_id: int) -> asyncio.Lock:
        return locks.setdefault(chat_id, asyncio.Lock())

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await guard(update) and update.effective_message:
            await update.effective_message.reply_text(HELP)

    async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update) or not update.effective_message:
            return
        async with lock_for(update.effective_chat.id):  # never reset in the middle of a request
            agent_for(update.effective_chat.id).reset()
        await update.effective_message.reply_text("Conversation cleared.")

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # effective_message also covers edited messages, where update.message is None.
        message = update.effective_message
        if message is None or not await guard(update):
            return
        text = clean_input(message.text)
        if text is None:
            return await message.reply_text("Empty or too long message (max 8000 characters).")
        if not limiter.allow(update.effective_user.id):
            return await message.reply_text("Rate limit reached, try again later.")
        chat_id = update.effective_chat.id
        lock = lock_for(chat_id)
        if lock.locked():
            await message.reply_text("Still working on your previous message; this one is next.")
        async with lock:
            await context.bot.send_chat_action(chat_id, "typing")
            try:
                reply = await asyncio.to_thread(agent_for(chat_id).ask, text)
            except Exception as exc:  # API or Docker problems: tell the user, keep the bot alive
                log.exception("request failed")
                return await message.reply_text(f"Something went wrong ({type(exc).__name__}). Try again.")
        footer = f"\n\n⚙️ {len(reply.runs)} sandbox run(s)" if reply.runs else ""
        for part in chunks(reply.text + footer):
            await message.reply_text(part)

    app = ApplicationBuilder().token(settings.telegram_token).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
