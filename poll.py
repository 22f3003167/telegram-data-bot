"""Run the bot locally with no public URL, no webhook, no hosting.

Long-polls Telegram's getUpdates and answers each message with the same
handler main.py's webhook uses, so behaviour is identical either way.

    python poll.py
"""

import os
import sys
import time
import traceback

import requests
from dotenv import load_dotenv

load_dotenv()

from main import API, BOT_TOKEN, handle  # noqa: E402

POLL_TIMEOUT = 30


def drop_webhook():
    """getUpdates and a registered webhook are mutually exclusive."""
    try:
        requests.post(f"{API}/deleteWebhook", timeout=15)
    except Exception:  # noqa: BLE001
        pass


def main():
    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set (copy .env.example to .env)")

    me = requests.get(f"{API}/getMe", timeout=15).json()
    if not me.get("ok"):
        sys.exit(f"bad token: {me}")
    print(f"polling as @{me['result']['username']} - Ctrl-C to stop", flush=True)

    drop_webhook()
    offset = None

    while True:
        try:
            resp = requests.get(
                f"{API}/getUpdates",
                params={"timeout": POLL_TIMEOUT, "offset": offset},
                timeout=POLL_TIMEOUT + 15,
            ).json()
        except Exception as exc:  # noqa: BLE001 - keep polling through blips
            print(f"[poll error] {exc}", flush=True)
            time.sleep(3)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or update.get("edited_message") or {}
            chat_id = (message.get("chat") or {}).get("id")
            text = message.get("text")
            if not (chat_id and text):
                continue
            print(f"[{chat_id}] {text[:70]}", flush=True)
            try:
                handle(chat_id, text)
            except Exception:  # noqa: BLE001 - one bad question must not stop the bot
                traceback.print_exc()


if __name__ == "__main__":
    main()
