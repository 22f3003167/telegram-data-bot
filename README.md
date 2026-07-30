# Telegram data-analyst bot

Answers one data-analysis question per Telegram message. An LLM agent drives two
tools — `fetch_url` and a sandboxed `run_python` — and every step of the run is
written to a public JSONL log.

## LLM endpoint

Any OpenAI-compatible endpoint, set via `LLM_BASE_URL` / `LLM_API_KEY` /
`LLM_MODEL`. Default is AI Pipe's **`https://aipipe.org/openai/v1`**.

Note that AI Pipe's other two routes are currently unusable: `/openrouter/v1`
returns `402 Insufficient credits` (AI Pipe's own upstream OpenRouter balance,
not the caller's), and `/geminiv1beta` rejects every current model with
`"pricing unknown"`. Use the `/openai/v1` route.

## Reply contract

Exactly one JSON object, no prose or markdown fences:

```json
{"answer": <shaped exactly as the question asked>, "log_url": "https://.../run.jsonl"}
```

Set `REPLY_FORMAT=bare` to reply with just the answer value instead, for graders
that exact-match the whole reply against the requested shape.

## Layout

| File | Purpose |
| --- | --- |
| `main.py` | Flask webhook, per-`chat_id` history, Telegram `sendMessage` |
| `agent.py` | Tool definitions, system prompt, tool-calling loop |
| `run_logger.py` | JSONL run log + upload, returns the public URL |

`logging.py` would shadow the stdlib `logging` module, so the logger lives in
`run_logger.py`.

## Local run

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements.txt
cp .env.example .env   # fill in the real values
.venv/bin/python main.py
```

## Endpoints

- `POST /webhook` — Telegram updates. Returns 200 immediately and analyses in a
  background thread so Telegram does not retry mid-run.
- `GET /healthz` — 200 OK, for uptime pings and Render health checks.

## Deploy (Render)

Start command: `gunicorn main:app`. Set `TELEGRAM_BOT_TOKEN`,
`OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `GCS_BUCKET_NAME` and the log-storage
credential in the dashboard. Then register the webhook:

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" -d "url=https://<app>.onrender.com/webhook"
```
