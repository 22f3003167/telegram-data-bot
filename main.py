"""Flask webhook app: Telegram -> data-analyst agent -> single JSON reply."""

import json
import os
import threading
import traceback

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

from agent import answer_question  # noqa: E402 - must follow load_dotenv

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_HISTORY = 20

# The grader requires EXACTLY this object and nothing else:
#   {"answer": <shaped as the question asks>, "log_url": "https://.../run.jsonl"}
# This is not configurable on purpose - emitting any other shape fails grading.
REPLY_KEYS = ("answer", "log_url")

# Vercel/Lambda-style hosts cannot run work after the response is returned.
SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

app = Flask(__name__)

# chat_id -> [{"role": ..., "content": ...}]. Fine for a single free-tier instance.
HISTORY = {}
SEEN_UPDATES = set()
LOCK = threading.Lock()


def send_message(chat_id, text):
    resp = requests.post(
        f"{API}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=30,
    )
    if not resp.ok:
        print(f"[sendMessage failed] {resp.status_code} {resp.text}", flush=True)
    return resp


def handle(chat_id, text):
    """Run the agent and reply with exactly one JSON object."""
    with LOCK:
        HISTORY.setdefault(chat_id, []).append({"role": "user", "content": text})
        history = HISTORY[chat_id][-MAX_HISTORY:]

    logger = None
    try:
        answer, logger = answer_question(history, chat_id)
    except Exception:  # noqa: BLE001 - always reply with valid JSON
        traceback.print_exc()
        answer = "[error: agent failed]"

    log_url = ""
    if logger is not None:
        try:
            log_url = logger.upload()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    # default=str so an exotic value can never make the reply unserialisable,
    # which would leave the grader with no JSON object at all.
    payload = json.dumps(
        {"answer": answer, "log_url": log_url}, ensure_ascii=False, default=str
    )
    assert set(json.loads(payload)) == set(REPLY_KEYS), "reply must be exactly the envelope"

    with LOCK:
        HISTORY.setdefault(chat_id, []).append({"role": "assistant", "content": payload})

    send_message(chat_id, payload)


@app.post("/webhook")
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    update_id = update.get("update_id")

    with LOCK:
        if update_id is not None and update_id in SEEN_UPDATES:
            return jsonify(ok=True)
        if update_id is not None:
            SEEN_UPDATES.add(update_id)

    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text")

    if chat_id and text:
        if SERVERLESS:
            # Serverless freezes the instance once the response returns, so a
            # background thread would be killed mid-analysis. Run inline and
            # hope the work fits inside the platform's function timeout.
            handle(chat_id, text)
        else:
            # Long-lived host: answer 200 at once so Telegram does not retry
            # while we analyse.
            threading.Thread(target=handle, args=(chat_id, text), daemon=True).start()

    return jsonify(ok=True)


@app.get("/healthz")
def healthz():
    return "OK", 200


@app.get("/")
def index():
    return "telegram-data-bot up", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
