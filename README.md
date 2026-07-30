# Telegram data-analyst bot

Answers one data-analysis question per Telegram message. An LLM agent drives
`web_search`, `fetch_url`, a sandboxed `run_python`, and the official **MoSPI MCP
server** — and every step of the run is written to a public JSONL log.

## Reply contract

Every reply is exactly one JSON object, no prose and no markdown fences:

```json
{"answer": <shaped exactly as the question asked>, "log_url": "https://.../run.jsonl"}
```

The shape is enforced in code (`main.py`) and is not configurable. `answer` is
whatever the question asked for — a number, string, array, or object with
specific keys — parsed from the question text itself, not a fixed schema.

## What it handles

- **Inline data** — figures embedded in the message, computed with pandas rather
  than mental arithmetic.
- **Public datasets** — a URL in the message, or one found with `web_search`,
  fetched and analysed. CSV and XLSX are parsed on arrival; the raw file is kept
  in the run's working directory so `run_python` can load it in full.
- **Multi-turn** — history is tracked per `chat_id`. The last message is the
  question; earlier turns are context (e.g. which dataset is under discussion).

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill in the values
.venv/bin/python poll.py
```

`poll.py` long-polls Telegram's `getUpdates`, so the bot runs from a laptop with
**no public URL, no webhook and no hosting**. It answers messages using exactly
the same handler as the webhook, so behaviour is identical.

`main.py` is the same bot as a Flask webhook app (`POST /webhook`,
`GET /healthz`) for when a public URL is available. Use one or the other —
Telegram does not allow a webhook and `getUpdates` at the same time, so `poll.py`
clears any registered webhook on startup.

## Layout

| File | Purpose |
| --- | --- |
| `agent.py` | Tool definitions, system prompt, tool-calling loop |
| `run_logger.py` | JSONL run log + upload, returns the public URL |
| `poll.py` | Long-polling runner — no hosting required |
| `main.py` | Flask webhook app, history, reply-shape enforcement |
| `test_agent.py` | Three quick end-to-end checks |
| `run_evals.py` | Scores the bot with the course grading pipeline |

`logging.py` would shadow the stdlib `logging` module, so the logger lives in
`run_logger.py`.

## Logs

Each run writes one JSON object per line — `run_start`, `user_question`,
`model_request`, `model_response`, `tool_call`, `tool_result`, `final_answer` —
and is committed to a public repo, giving a permanent wget-able URL:

```
https://raw.githubusercontent.com/<GITHUB_REPO>/main/logs/<chat_id>-<run_id>.jsonl
```

`GITHUB_REPO` must be a **separate** repo from this one; every run commits a
file, so pointing it here would add a commit per answer.

## Testing

```bash
.venv/bin/python test_agent.py

git clone https://github.com/Jivraj-18/tds-p1-t2-2026-telegram-bot ../pipeline
.venv/bin/python run_evals.py --pipeline ../pipeline
```

`run_evals.py` substitutes for the pipeline's `collect.py` — the only stage that
touches Telegram, and the one needing a logged-in user account — by calling the
agent directly. Questions are rendered with the same `Template.substitute` logic
and results written in `collect.py`'s format, so the pipeline's own `grade.py`
scores them unmodified.

## LLM endpoint

Any OpenAI-compatible endpoint, via `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`.
Default is AI Pipe's `https://aipipe.org/openai/v1`.

AI Pipe's other two routes are currently unusable: `/openrouter/v1` returns
`402 Insufficient credits` (AI Pipe's own upstream balance, not the caller's),
and `/geminiv1beta` rejects every current model with `"pricing unknown"`.

## Indian official statistics (MoSPI)

`mospi.gov.in` is JavaScript-rendered and serves no data over plain HTTP, so
scraping it does not work. Instead the agent talks to the National Statistics
Office's **MCP server** at `https://mcp.mospi.gov.in/mcp` (no authentication),
covering 25 datasets — PLFS, CPI, IIP, ASI, NAS, NFHS, GENDER, UDISE and more.

Its tools are discovered at runtime via `tools/list` and registered as
`mospi_list_datasets`, `mospi_get_indicators`, `mospi_get_metadata` and
`mospi_get_data`, so new tools appear automatically. If the server is
unreachable the agent runs without them rather than failing the question.

Override the endpoint with `MOSPI_MCP_URL`.

## Known limitations
- **Search throttling.** DuckDuckGo rate-limits bursts. `web_search` retries
  across two endpoints and then says so explicitly, so the model stops retrying
  and works from a landing page or its own knowledge instead.
- **History is in-process.** Fine for one long-running instance; it does not
  survive a restart or spread across replicas.
