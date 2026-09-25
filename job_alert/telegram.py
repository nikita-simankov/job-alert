"""Minimal Telegram Bot API client (long polling + sendMessage) and message formatting."""

from __future__ import annotations

import logging

import httpx

from .sources import Posting

log = logging.getLogger(__name__)

# Characters that legacy Markdown (parse_mode=Markdown) treats as markup.
_MARKDOWN_SPECIAL = ("\\", "_", "*", "`", "[")
SNIPPET_LIMIT = 280


class TelegramError(RuntimeError):
    pass


def escape_markdown(text: str) -> str:
    for char in _MARKDOWN_SPECIAL:
        text = text.replace(char, "\\" + char)
    return text


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_notification(posting: Posting, score: int, reason: str) -> str:
    lines = [
        f"🆕 New match: {score}% compatible",
        f"*{escape_markdown(posting.title)}* @ {escape_markdown(posting.company or 'Unknown company')}",
    ]
    meta = [f"📍 {posting.location}" if posting.location else "", f"💰 {posting.salary}" if posting.salary else ""]
    meta = [m for m in meta if m]
    if meta:
        lines.append(escape_markdown(" | ".join(meta)))
    if posting.snippet:
        lines.append(escape_markdown(truncate(posting.snippet, SNIPPET_LIMIT)))
    lines.append(f"✅ Why it matches: {escape_markdown(reason)}")
    lines.append(f"🔗 [View posting]({posting.url})")
    return "\n".join(lines)


class TelegramBot:
    def __init__(self, http: httpx.AsyncClient, token: str):
        self.http = http
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"

    async def _call(self, method: str, payload: dict, timeout: float = 30) -> dict | list | bool:
        try:
            response = await self.http.post(f"{self.base}/{method}", json=payload, timeout=timeout)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Never log the URL: it contains the bot token.
            raise TelegramError(f"{method}: {exc.__class__.__name__}") from exc
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('error_code')} {data.get('description')}")
        return data["result"]

    async def send_message(self, chat_id: int, text: str, markdown: bool = True) -> None:
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if markdown:
            payload["parse_mode"] = "Markdown"
        try:
            await self._call("sendMessage", payload)
        except TelegramError as exc:
            # A formatting slip must not block the notification queue forever:
            # resend as plain text if Telegram couldn't parse the markup.
            if not markdown or "can't parse entities" not in str(exc):
                raise
            payload.pop("parse_mode")
            await self._call("sendMessage", payload)

    async def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        payload: dict = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return await self._call("getUpdates", payload, timeout=timeout + 10)  # type: ignore[return-value]

    async def download_text_file(self, file_id: str, max_bytes: int = 200_000) -> str:
        info = await self._call("getFile", {"file_id": file_id})
        path = info.get("file_path") if isinstance(info, dict) else None
        if not path:
            raise TelegramError("getFile: no file_path")
        if (info.get("file_size") or 0) > max_bytes:
            raise TelegramError("file too large")
        try:
            response = await self.http.get(f"{self.file_base}/{path}", timeout=30)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TelegramError(f"file download: {exc.__class__.__name__}") from exc
        return response.content.decode("utf-8", errors="replace")
