"""A minimal Telegram Bot API client on the standard library.

Long-polling only, no webhooks, so it needs no open inbound port — which is
exactly why the bot lives on the foreign server and sidesteps the Iranian
datacenter's port blocking entirely.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import config


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str):
        if not token:
            raise TelegramError("bot token is empty")
        self._base = f"https://api.telegram.org/bot{token}"
        self._opener = self._build_opener()

    def _build_opener(self):
        handlers = []
        if config.TELEGRAM_PROXY:
            handlers.append(urllib.request.ProxyHandler({
                "https": config.TELEGRAM_PROXY, "http": config.TELEGRAM_PROXY,
            }))
        return urllib.request.build_opener(*handlers)

    def _call(self, method: str, params: dict[str, Any], timeout: int = 65) -> Any:
        data = urllib.parse.urlencode(
            {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
             for k, v in params.items() if v is not None}
        ).encode()
        req = urllib.request.Request(f"{self._base}/{method}", data=data)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise TelegramError(f"{method} HTTP {exc.code}: {body[:200]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramError(f"{method} network error: {exc}") from exc
        if not payload.get("ok"):
            raise TelegramError(f"{method} failed: {payload.get('description')}")
        return payload.get("result")

    # -- polling ----------------------------------------------------------
    def get_updates(self, offset: int, timeout: int = 50) -> list[dict[str, Any]]:
        return self._call("getUpdates",
                          {"offset": offset, "timeout": timeout,
                           "allowed_updates": ["message", "callback_query"]},
                          timeout=timeout + 15)

    # -- sending ----------------------------------------------------------
    def send_message(self, chat_id: int, text: str,
                     keyboard: list | None = None,
                     parse_mode: str = "Markdown") -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text,
                                  "disable_web_page_preview": True}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if keyboard is not None:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return self._call("sendMessage", params)

    def edit_message(self, chat_id: int, message_id: int, text: str,
                     keyboard: list | None = None,
                     parse_mode: str = "Markdown") -> Any:
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id,
                                  "text": text, "parse_mode": parse_mode,
                                  "disable_web_page_preview": True}
        if keyboard is not None:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            return self._call("editMessageText", params)
        except TelegramError as exc:
            # Editing to identical content errors; that is harmless.
            if "not modified" in str(exc):
                return None
            raise

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery",
                      {"callback_query_id": callback_id, "text": text}, timeout=15)
        except TelegramError:
            pass

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe", {}, timeout=15)
