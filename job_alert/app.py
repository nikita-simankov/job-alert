"""The bot: polls sources, scores new postings, notifies, answers commands, sends a daily heartbeat."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .config import Config
from .db import Store
from .scorer import Scorer, ScoringError, ScoringRefused
from .sources import HHClient, SourceError
from .telegram import TelegramBot, TelegramError, format_notification

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 3600
HELP_TEXT = (
    "Commands:\n"
    "/status - what the bot is doing\n"
    "/resume <text> - set the resume used for scoring (or send a .txt file)\n"
    "/help - this message"
)


@dataclass
class SourceState:
    failures: int = 0
    next_attempt: float = 0.0
    last_error: str = ""
    last_success: str = ""


class JobAlertBot:
    def __init__(self, config: Config, store: Store, http: httpx.AsyncClient, scorer: Scorer | None = None):
        self.config = config
        self.store = store
        self.hh = HHClient(http, config.hh_user_agent, config.hh_access_token)
        self.telegram = TelegramBot(http, config.telegram_bot_token)
        self.scorer = scorer or Scorer(config.anthropic_api_key, config.claude_model, config.claude_effort)
        self.tz = ZoneInfo(config.timezone)
        self.source_state = {source: SourceState() for source in config.sources}

    # --- helpers ------------------------------------------------------------

    @property
    def chat_id(self) -> int | None:
        if self.config.telegram_chat_id is not None:
            return self.config.telegram_chat_id
        stored = self.store.get_setting("chat_id")
        return int(stored) if stored else None

    def resume(self) -> str:
        stored = self.store.get_setting("resume")
        if stored:
            return stored
        if self.config.resume_text:
            return self.config.resume_text
        if self.config.resume_file.is_file():
            return self.config.resume_file.read_text(encoding="utf-8").strip()
        return ""

    async def notify(self, text: str, markdown: bool = False) -> bool:
        chat_id = self.chat_id
        if chat_id is None:
            return False
        try:
            await self.telegram.send_message(chat_id, text, markdown=markdown)
            return True
        except TelegramError as exc:
            log.warning("Could not send message: %s", exc)
            return False

    # --- polling ------------------------------------------------------------

    async def poll_source(self, source: str) -> None:
        """One poll of one source. Raises SourceError if the search itself fails."""
        postings = await self.hh.search(source, self.config.filters[source], self.config.per_page)
        self.store.bump("checks")

        seen = self.store.seen_keys([p.key for p in postings])
        new, batch_keys = [], set()
        for posting in postings:
            if posting.key not in seen and posting.key not in batch_keys:
                new.append(posting)
                batch_keys.add(posting.key)

        if not self.store.has_source_history(source):
            # First run: score only the newest few, mark the rest as seen unscored.
            to_score = new[: self.config.first_run_batch]
            for posting in new[self.config.first_run_batch :]:
                self.store.record(
                    key=posting.key, source=source, external_id=posting.external_id,
                    title=posting.title, url=posting.url, status="skipped_initial",
                )
            self.store.mark_source_initialized(source)
            log.info("%s: first run, scoring %d of %d open postings", source, len(to_score), len(new))
        else:
            to_score = new

        if not to_score:
            return
        resume = self.resume()
        if not resume:
            log.warning("%s: %d new postings waiting, but no resume is set", source, len(to_score))
            return

        for posting in to_score:
            await self.score_and_store(source, posting, resume)

    async def score_and_store(self, source: str, posting, resume: str) -> None:
        # Any failure here leaves the posting unrecorded, so it is retried next cycle.
        try:
            description = await self.hh.description(source, posting.external_id)
        except SourceError as exc:
            log.warning("%s: could not fetch vacancy %s: %s", source, posting.external_id, exc)
            self.store.bump("errors")
            return
        try:
            result = await self.scorer.score(resume, posting, description)
        except ScoringError as exc:
            log.warning("%s: scoring vacancy %s failed, will retry: %s", source, posting.external_id, exc)
            self.store.bump("errors")
            return
        except ScoringRefused as exc:
            log.warning("%s: vacancy %s not scored: %s", source, posting.external_id, exc)
            if self.store.record(
                key=posting.key, source=source, external_id=posting.external_id,
                title=posting.title, url=posting.url, status="refused",
            ):
                self.store.bump("new_postings")
            return

        log.info("%s: %s @ %s scored %d", source, posting.title, posting.company, result.score)
        if self.store.record(
            key=posting.key, source=source, external_id=posting.external_id,
            title=posting.title, url=posting.url, status="scored",
            score=result.score, reason=result.reason,
            message=format_notification(posting, result.score, result.reason),
        ):
            self.store.bump("new_postings")

    async def flush_notifications(self) -> None:
        chat_id = self.chat_id
        if chat_id is None:
            return
        for pending in self.store.pending_notifications(self.config.threshold):
            try:
                await self.telegram.send_message(chat_id, pending.message)
            except TelegramError as exc:
                log.warning("Notification for %s not sent, will retry: %s", pending.key, exc)
                self.store.bump("errors")
                return
            self.store.mark_notified(pending.key)
            self.store.bump("notifications")
            await asyncio.sleep(1)  # stay well under Telegram's per-chat rate limit

    async def poll_once(self) -> None:
        now = time.monotonic()
        for source in self.config.sources:
            state = self.source_state[source]
            if now < state.next_attempt:
                continue
            try:
                await self.poll_source(source)
            except SourceError as exc:
                # Sources fail independently; back off this one and keep going.
                state.failures += 1
                state.last_error = str(exc)
                delay = min(self.config.poll_interval_seconds * 2 ** (state.failures - 1), MAX_BACKOFF_SECONDS)
                state.next_attempt = time.monotonic() + delay
                self.store.bump("errors")
                log.warning("%s: poll failed (%d in a row), next try in %ds: %s", source, state.failures, delay, exc)
            else:
                state.failures = 0
                state.last_error = ""
                state.next_attempt = 0.0
                state.last_success = datetime.now(self.tz).strftime("%H:%M")
        await self.flush_notifications()

    async def poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # keep the loop alive whatever happens
                log.exception("Unexpected error in poll loop")
                self.store.bump("errors")
            await asyncio.sleep(self.config.poll_interval_seconds)

    # --- Telegram commands ----------------------------------------------------

    async def telegram_loop(self) -> None:
        offset_raw = self.store.get_setting("telegram_offset")
        offset = int(offset_raw) if offset_raw else None
        while True:
            try:
                updates = await self.telegram.get_updates(offset)
            except TelegramError as exc:
                log.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(10)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                self.store.set_setting("telegram_offset", str(offset))
                try:
                    await self.handle_message(update.get("message") or {})
                except Exception:
                    log.exception("Error handling Telegram update")

    async def handle_message(self, message: dict) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            return
        text = (message.get("text") or message.get("caption") or "").strip()
        command = text.split(maxsplit=1)[0].split("@")[0].lower() if text else ""

        owner = self.chat_id
        if owner is None:
            if command != "/start":
                return
            # The first person to /start becomes the only user.
            self.store.set_setting("chat_id", str(chat_id))
            log.info("Registered chat %s as the bot owner", chat_id)
            note = "" if self.resume() else "\n\nNo resume yet: send /resume followed by your resume text."
            await self.telegram.send_message(chat_id, "Registered. You'll get job alerts here." + note + "\n\n" + HELP_TEXT, markdown=False)
            await self.flush_notifications()
            return
        if chat_id != owner:
            return  # single-user bot: ignore everyone else

        document = message.get("document")
        if document and (command == "/resume" or (document.get("file_name") or "").lower().endswith(".txt")):
            try:
                resume = (await self.telegram.download_text_file(document["file_id"])).strip()
            except TelegramError as exc:
                await self.telegram.send_message(chat_id, f"Couldn't read that file: {exc}", markdown=False)
                return
            await self.save_resume(chat_id, resume)
        elif command == "/resume":
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await self.telegram.send_message(
                    chat_id, "Send /resume followed by your resume text, or attach a .txt file.", markdown=False
                )
                return
            await self.save_resume(chat_id, parts[1].strip())
        elif command == "/status":
            await self.telegram.send_message(chat_id, self.status_text(), markdown=False)
        elif command in ("/start", "/help"):
            await self.telegram.send_message(chat_id, HELP_TEXT, markdown=False)

    async def save_resume(self, chat_id: int, resume: str) -> None:
        if len(resume) < 50:
            await self.telegram.send_message(chat_id, "That looks too short to be a resume, not saved.", markdown=False)
            return
        self.store.set_setting("resume", resume)
        await self.telegram.send_message(
            chat_id, f"Resume saved ({len(resume)} characters). New postings will be scored against it.", markdown=False
        )

    def status_text(self) -> str:
        counters = self.store.counters()
        lines = [
            f"Since last heartbeat: {counters['checks']} checks, {counters['new_postings']} new postings, "
            f"{counters['notifications']} notifications, {counters['errors']} errors.",
            f"Threshold: {self.config.threshold}%. Poll interval: {self.config.poll_interval_seconds // 60} min.",
            "Resume: " + ("set" if self.resume() else "NOT SET, nothing is being scored"),
        ]
        for source, state in self.source_state.items():
            if state.failures:
                lines.append(f"{source}: failing ({state.failures} in a row): {state.last_error[:200]}")
            else:
                lines.append(f"{source}: ok" + (f", last check {state.last_success}" if state.last_success else ""))
        return "\n".join(lines)

    # --- heartbeat ------------------------------------------------------------

    async def heartbeat_once(self) -> None:
        now = datetime.now(self.tz)
        today = now.date().isoformat()
        if now.hour < self.config.heartbeat_hour or self.store.get_setting("last_heartbeat") == today:
            return
        if await self.notify("💓 Still running.\n" + self.status_text()):
            self.store.set_setting("last_heartbeat", today)
            self.store.reset_counters()

    async def heartbeat_loop(self) -> None:
        while True:
            try:
                await self.heartbeat_once()
            except Exception:
                log.exception("Unexpected error in heartbeat loop")
            await asyncio.sleep(60)

    async def run(self) -> None:
        log.info("Starting: sources=%s, threshold=%d%%, model=%s", self.config.sources, self.config.threshold, self.config.claude_model)
        await self.notify("🚀 Job alert bot started.")
        await asyncio.gather(self.poll_loop(), self.telegram_loop(), self.heartbeat_loop())
