"""Tool-calling data-analyst agent backed by OpenRouter (OpenAI-compatible API)."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile

import pandas as pd
import requests
from openai import OpenAI

from run_logger import RunLogger

MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
MAX_STEPS = 14
PYTHON_TIMEOUT = 30
PREVIEW_CHARS = 12000

SYSTEM_PROMPT = """You are a rigorous data analyst answering one question per conversation.

RULES:
1. Never guess at data. If the question references a dataset, a URL, or a public
   source (MOSPI, data.gov.in, RBI, census, etc.), use fetch_url to actually
   retrieve it, then use run_python to analyse it. If the data is embedded
   directly in the message text, use run_python to compute on it rather than
   doing arithmetic in your head.
2. Read the user's message carefully to identify the EXACT JSON shape it asks
   for. It may ask for a number, a string, a list, a list of lists, or an object
   with specific keys. Match that shape and those key names precisely. Respect
   any stated rounding, units, ordering, or formatting instructions.
3. In a multi-turn conversation, answer the LAST user message. Use earlier
   messages only as context (e.g. which dataset is under discussion).
4. When you are done, reply with ONLY the JSON value that belongs under the
   "answer" key. No prose, no explanation, no markdown code fences.
   - If the answer is a number, output just the number, e.g. 42.7
   - If it is a string, output a valid JSON string, e.g. "Maharashtra"
   - If it is a list, output a JSON array, e.g. [1, 2, 3]
   - If it is an object, output a JSON object with exactly the requested keys.
   Do not wrap it in {"answer": ...} — the calling code does that.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "HTTP GET a URL and return its decoded content. Handles CSV, XLSX, "
                "JSON, HTML and plain text. The full file is also saved into the "
                "working directory so run_python can load it; the returned text "
                "reports the local filename and a preview."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch."}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute Python in a sandboxed subprocess with a 30s timeout. "
                "pandas (pd), numpy (np), json, math and re are pre-imported. The "
                "working directory contains any files saved by fetch_url. Use "
                "print() to return values — stdout is what you get back."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to run."}
                },
                "required": ["code"],
            },
        },
    },
]

PREAMBLE = (
    "import json, math, re, sys\n"
    "import pandas as pd\n"
    "import numpy as np\n"
    "pd.set_option('display.max_columns', 200)\n"
    "pd.set_option('display.width', 200)\n"
)


def _truncate(text, limit=PREVIEW_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated, {len(text)} chars total]"


def _safe_name(url):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", url.split("?")[0].split("/")[-1])
    return name[-80:] or "download.bin"


def fetch_url(url, workdir):
    """Fetch a URL, persist it into workdir, return a textual preview."""
    resp = requests.get(
        url,
        timeout=60,
        headers={"User-Agent": "Mozilla/5.0 (compatible; tds-data-bot/1.0)"},
    )
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "").lower()
    name = _safe_name(url)
    path = os.path.join(workdir, name)
    with open(path, "wb") as fh:
        fh.write(resp.content)

    lower = url.lower().split("?")[0]
    header = f"Saved to local file: {name} ({len(resp.content)} bytes, Content-Type: {ctype})\n"

    if lower.endswith((".xlsx", ".xls")) or "spreadsheet" in ctype or "excel" in ctype:
        try:
            sheets = pd.read_excel(io.BytesIO(resp.content), sheet_name=None)
            parts = [header, f"Excel workbook with sheets: {list(sheets)}"]
            for sheet, df in sheets.items():
                parts.append(
                    f"\n--- sheet '{sheet}' shape={df.shape} ---\n"
                    f"columns: {list(df.columns)}\n{df.head(20).to_string()}"
                )
            return _truncate("\n".join(parts))
        except Exception as exc:  # noqa: BLE001 - report to the model, keep going
            return header + f"[could not parse as Excel: {exc}] Load it in run_python."

    if lower.endswith(".csv") or "csv" in ctype:
        try:
            df = pd.read_csv(path)
            return _truncate(
                header
                + f"Parsed CSV shape={df.shape}\ncolumns: {list(df.columns)}\n"
                + df.head(30).to_string()
            )
        except Exception as exc:  # noqa: BLE001
            return header + f"[could not parse as CSV: {exc}]\n" + _truncate(resp.text)

    return _truncate(header + resp.text)


def run_python(code, workdir):
    """Run code in a subprocess. Never raises; returns stdout/stderr as text."""
    script = os.path.join(workdir, "_snippet.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(PREAMBLE + "\n" + code)
    try:
        proc = subprocess.run(
            [sys.executable, script],
            capture_output=True,
            text=True,
            timeout=PYTHON_TIMEOUT,
            cwd=workdir,
        )
    except subprocess.TimeoutExpired:
        return f"[ERROR] Execution exceeded {PYTHON_TIMEOUT}s and was killed."
    except Exception as exc:  # noqa: BLE001
        return f"[ERROR] Could not run subprocess: {exc}"

    out = _truncate(proc.stdout.strip())
    err = _truncate(proc.stderr.strip(), 4000)
    if proc.returncode != 0:
        return f"[exit {proc.returncode}]\nSTDOUT:\n{out}\nSTDERR:\n{err}"
    return out + (f"\n[stderr]\n{err}" if err else "") or "[no output — use print()]"


def _parse_answer(text):
    """Coerce the model's final message into a JSON value."""
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except (ValueError, TypeError):
        return cleaned


def _client():
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
        default_headers={"X-Title": "tds-telegram-data-bot"},
    )


def answer_question(history, chat_id):
    """Run the tool-calling loop.

    history: list of {"role": "user"|"assistant", "content": str}, oldest first,
             with the LAST entry being the question to answer.
    Returns (answer_value, run_logger).
    """
    logger = RunLogger(chat_id)
    workdir = tempfile.mkdtemp(prefix=f"work-{logger.run_id}-")
    logger.log("run_start", model=MODEL, workdir=workdir, turns=len(history))

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend({"role": m["role"], "content": m["content"]} for m in history)
    logger.log("user_question", content=history[-1]["content"] if history else "")

    client = _client()
    answer = None

    for step in range(MAX_STEPS):
        logger.log("model_request", step=step, message_count=len(messages))
        try:
            resp = client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS, temperature=0
            )
        except Exception as exc:  # noqa: BLE001
            logger.log("model_error", step=step, error=str(exc))
            answer = f"[error contacting model: {exc}]"
            break

        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []
        logger.log(
            "model_response",
            step=step,
            content=msg.content,
            tool_calls=[
                {"name": tc.function.name, "arguments": tc.function.arguments}
                for tc in tool_calls
            ],
        )

        if not tool_calls:
            answer = _parse_answer(msg.content or "")
            logger.log("final_answer", answer=answer)
            break

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except ValueError:
                args = {}
            logger.log("tool_call", step=step, tool=name, arguments=args)
            try:
                if name == "fetch_url":
                    result = fetch_url(args.get("url", ""), workdir)
                elif name == "run_python":
                    result = run_python(args.get("code", ""), workdir)
                else:
                    result = f"[ERROR] Unknown tool: {name}"
            except Exception as exc:  # noqa: BLE001
                result = f"[ERROR] {type(exc).__name__}: {exc}"
            logger.log("tool_result", step=step, tool=name, result=result)
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result}
            )
    else:
        answer = "[error: exceeded maximum tool-calling steps without a final answer]"
        logger.log("final_answer", answer=answer, note="step limit reached")

    return answer, logger
