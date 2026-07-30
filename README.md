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

This shape is enforced in code and is not configurable — the grader requires
exactly these two keys.

Note that the `grade.py` in the reference eval repo
(`Jivraj-18/tds-p1-t2-2026-telegram-bot`) exact-matches the *whole* reply against
the expected value, so it scores this envelope as wrong. The real grader unwraps
`answer`; when testing against that repo locally, unwrap before comparing.

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

## Deploy

Environment variables, either host: `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`,
`LLM_BASE_URL`, `LLM_MODEL`, `LOG_BACKEND=github`, `GITHUB_TOKEN`, `GITHUB_REPO`.

**Vercel** — `vercel.json` and `api/index.py` are committed; import the repo and
add the env vars. `maxDuration` is set to 60s, the Hobby ceiling.

**Render** — start command `gunicorn main:app`.

Then register the webhook:

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" -d "url=https://<host>/webhook"
```

### Vercel caveats

The app detects `VERCEL` and processes the question inline, because a serverless
instance is frozen once its response returns and a background thread would be
killed mid-run. Two consequences:

- **60s ceiling.** Measured runs: ~6-7s for a straightforward CSV question, but
  **39.7s** for one needing search plus multiple fetches, and that same question
  has also exhausted its step budget. Slow questions can exceed the limit, and
  the run is then lost entirely.
- **History is per-instance.** `HISTORY` is an in-memory dict. Serverless gives
  no guarantee that consecutive turns hit the same instance, so multi-turn
  context can be silently lost. A long-lived host keeps one process and does not
  have this problem.

Neither applies on Render's free tier, which runs a single persistent instance.
