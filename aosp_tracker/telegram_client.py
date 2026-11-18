"""Telegram Bot API Client for Sending and Editing Live Status Messages.

Implements rate limiting, backoff, and robust error handling using standard
Python library components.
"""

import json
import time
import urllib.parse
import urllib.request
import urllib.error
import logging
import os
from typing import Optional, Dict, Any, List

logger = logging.getLogger("aosp_tracker.telegram")


class TelegramClient:
    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self._last_edit_time: Dict[str, float] = {}
        self.chat_migrations: Dict[str, str] = {
            "-5377513002": "-1004371923194",
        }

    def resolve_chat_id(self, chat_id: Any) -> str:
        """Resolve migrated chat IDs (e.g. upgraded basic group to supergroup)."""
        cid = str(chat_id).strip()
        return self.chat_migrations.get(cid, cid)

    def _post(self, method: str, data: Dict[str, Any], timeout: int = 20) -> Optional[Dict[str, Any]]:
        """Perform POST request to Telegram API."""
        if "chat_id" in data:
            data["chat_id"] = self.resolve_chat_id(data["chat_id"])

        url = f"{self.base_url}/{method}"
        json_bytes = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=json_bytes,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8")
                err_data = json.loads(body)
                desc = err_data.get("description", "")
                # Ignore benign errors like "message is not modified"
                if "message is not modified" in desc.lower():
                    return {"ok": True, "result": True}

                # Auto-handle supergroup migration
                migrate_to = err_data.get("parameters", {}).get("migrate_to_chat_id")
                if migrate_to:
                    old_id = str(data.get("chat_id", ""))
                    new_id = str(migrate_to)
                    logger.info("Chat %s upgraded to supergroup %s. Auto-redirecting.", old_id, new_id)
                    self.chat_migrations[old_id] = new_id
                    data["chat_id"] = new_id
                    return self._post(method, data, timeout)

                logger.warning("Telegram API HTTP %d for %s: %s", e.code, method, desc)
                if e.code == 429:
                    retry_after = err_data.get("parameters", {}).get("retry_after", 3)
                    time.sleep(retry_after)
            except Exception:
                logger.warning("Telegram API HTTP %d for %s", e.code, method)
            return None
        except Exception as e:
            logger.warning("Telegram API request error for %s: %s", method, e)
            return None

    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = False,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Send message to chat, returning message_id if successful."""
        data: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup:
            data["reply_markup"] = reply_markup

        resp = self._post("sendMessage", data)
        if resp and resp.get("ok"):
            msg_id = resp.get("result", {}).get("message_id")
            if msg_id:
                self._last_edit_time[f"{chat_id}_{msg_id}"] = time.time()
                return msg_id
        return None

    def edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = False,
        min_interval_seconds: float = 3.0,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Edit an existing message, respecting minimum update intervals."""
        key = f"{chat_id}_{message_id}"
        now = time.time()
        last_time = self._last_edit_time.get(key, 0.0)

        # Enforce minimum interval to prevent Telegram rate limits
        if (now - last_time) < min_interval_seconds:
            return False

        data: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup:
            data["reply_markup"] = reply_markup

        resp = self._post("editMessageText", data)
        if resp and resp.get("ok"):
            self._last_edit_time[key] = now
            return True
        return False

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> bool:
        """Acknowledge an incoming callback query from an inline button."""
        data: Dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text:
            data["text"] = text
        resp = self._post("answerCallbackQuery", data, timeout=10)
        return bool(resp and resp.get("ok"))

    def get_me(self) -> Optional[Dict[str, Any]]:
        """Fetch bot user information."""
        return self._post("getMe", {}, timeout=10)

    def delete_message(self, chat_id: str, message_id: int) -> bool:
        """Delete a message from chat."""
        resp = self._post("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=10)
        return bool(resp and resp.get("ok"))

    def get_updates(self, offset: Optional[int] = None, timeout: int = 25) -> List[Dict[str, Any]]:
        """Long poll Telegram getUpdates endpoint."""
        data: Dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            data["offset"] = offset
        resp = self._post("getUpdates", data, timeout=timeout + 10)
        if resp and resp.get("ok"):
            return resp.get("result", [])
        raise ConnectionError("Telegram getUpdates failed")

    def set_my_commands(self, commands: List[Dict[str, str]]) -> bool:
        """Set the bot's registered command list in Telegram UI."""
        resp = self._post("setMyCommands", {"commands": commands})
        return bool(resp and resp.get("ok"))

    def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
        custom_filename: Optional[str] = None,
        timeout: int = 60,
    ) -> Optional[int]:
        """Send a document/file to Telegram chat (e.g. error.log)."""
        chat_id = self.resolve_chat_id(chat_id)
        boundary = "----TelegramBoundary" + os.urandom(16).hex()
        filename = custom_filename or os.path.basename(file_path)
        filename = filename.translate(str.maketrans({'"': "_", "\\": "_", "\r": "_", "\n": "_"}))
        fields = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption
        if reply_to_message_id:
            fields["reply_to_message_id"] = str(reply_to_message_id)
        preamble = "".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            for name, value in fields.items()
        )
        preamble += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                     "Content-Type: text/plain\r\n\r\n")
        prefix = preamble.encode()
        ending = f"\r\n--{boundary}--\r\n".encode()
        try:
            with open(file_path, "rb") as stream:
                file_size = os.fstat(stream.fileno()).st_size
                length = len(prefix) + file_size + len(ending)

                def body():
                    yield prefix
                    remaining = file_size
                    while remaining:
                        chunk = stream.read(min(remaining, 256 * 1024))
                        if not chunk:
                            raise OSError("Log truncated during upload")
                        yield chunk
                        remaining -= len(chunk)
                    yield ending

                req = urllib.request.Request(
                    f"{self.base_url}/sendDocument", data=body(),
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                             "Content-Length": str(length)},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    result = json.load(resp)
                if result.get("ok"):
                    return result.get("result", {}).get("message_id")
        except Exception as e:
            logger.warning("Failed to send document %s to chat %s: %s", file_path, chat_id, e)

        return None
