"""Tool-calling data-analyst agent backed by OpenRouter (OpenAI-compatible API)."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.parse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from openai import OpenAI

from run_logger import RunLogger

MODEL = os.environ.get("LLM_MODEL", "gpt-4.1-mini")
BASE_URL = os.environ.get("LLM_BASE_URL", "https://aipipe.org/openai/v1")
MAX_STEPS = 18  # the MoSPI MCP workflow alone is four calls before any analysis
PYTHON_TIMEOUT = 30
PREVIEW_CHARS = 12000

SYSTEM_PROMPT = """You are a rigorous data analyst answering one question per conversation.

RULES:
1. Never guess at data. If the question references a dataset, a URL, or a public
   source (MOSPI, data.gov.in, RBI, census, etc.), use fetch_url to actually
   retrieve it, then use run_python to analyse it. If the data is embedded
   directly in the message text, use run_python to compute on it rather than
   doing arithmetic in your head.
1a. NEVER invent or guess file URLs. If you do not have an exact URL from the
   user, use web_search to find it, then fetch_url the URL search returned. If a
   fetch fails, do NOT retry variations of the same guessed path (adding _0, _1,
   changing the year). Two failures on one approach means the approach is wrong:
   search again with different terms, or fetch the source's landing/downloads
   page and read the real links out of its HTML.
1b. You have a limited number of steps. Spend them on progress, not on retrying
   something that already failed. If you genuinely cannot retrieve the data,
   answer from your own knowledge in the requested shape rather than failing.
1d. For Indian official statistics (MoSPI / NSO / eSankhyiki - labour, prices,
   industry, health, gender, education, energy, environment) use the mospi_*
   tools, NOT fetch_url. mospi.gov.in is JavaScript-rendered and returns no data
   over plain HTTP, but the MCP server serves the same figures directly. The
   workflow is: mospi_list_datasets to pick a dataset, then mospi_get_indicators,
   then mospi_get_metadata for valid filter values, then mospi_get_data.
   mospi_get_data returns every state when you omit a state filter, so read the
   whole list out of that one response - never query states one at a time.
1e. Tool results that are too long to show in full are saved as files in the
   working directory, and the tool tells you the filename. Load those files in
   run_python (json.load(open('name.json'))) rather than retyping the data into
   your code - retyping is slow, error-prone, and only sees the truncated part.
1c. NEVER answer with a placeholder such as "unknown", "not found", "N/A" or
   "data unavailable". The question always has a real answer. If retrieval
   failed, give your single best real-world answer in the requested shape --
   a concrete state name, a concrete number. A wrong-but-plausible answer is
   strictly better than a placeholder.
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
5. Types matter, because answers are compared by exact match:
   - Numbers must be JSON numbers, never strings: 87.0 and 167, not "87.0" or
     "167". This applies to values inside objects and arrays too. Note that
     values coming back from an API are often strings - convert them.
   - Do not add thousands separators, currency symbols, units or percent signs
     to a numeric answer.
   - Only use a string when the answer really is text, such as a state name.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web and return the top results as title / URL / "
                "snippet. Use this to FIND the real URL of a dataset or report "
                "before fetching it. Never guess a file URL yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."}
                },
                "required": ["query"],
            },
        },
    },
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


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


SEARCH_ENDPOINTS = [
    "https://html.duckduckgo.com/html/",
    "https://lite.duckduckgo.com/lite/",
]


def _parse_results(html, max_results):
    soup = BeautifulSoup(html, "lxml")
    lines = []
    for node in soup.select(".result")[:max_results]:
        link = node.select_one(".result__a")
        if not link:
            continue
        snippet = node.select_one(".result__snippet")
        href = link.get("href", "")
        # DDG wraps some links as /l/?uddg=<encoded target>
        if "uddg=" in href:
            href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
        lines.append(
            f"- {link.get_text(strip=True)}\n  URL: {href}\n"
            f"  {snippet.get_text(strip=True) if snippet else ''}"
        )
    if lines:
        return lines
    # lite endpoint uses a plain table of links
    for a in soup.select("a.result-link")[:max_results]:
        lines.append(f"- {a.get_text(strip=True)}\n  URL: {a.get('href', '')}")
    return lines


def web_search(query, max_results=8):
    """Keyless web search. DuckDuckGo throttles bursts, so retry across
    endpoints and say so explicitly rather than looking like 'no results'."""
    for attempt, endpoint in enumerate(SEARCH_ENDPOINTS):
        if attempt:
            time.sleep(2)
        try:
            resp = requests.post(
                endpoint, data={"q": query}, headers={"User-Agent": UA}, timeout=30
            )
            resp.raise_for_status()
        except Exception:  # noqa: BLE001 - try the next endpoint
            continue
        lines = _parse_results(resp.text, max_results)
        if lines:
            return "\n".join(lines)

    return (
        "[search unavailable: the search backend is rate-limiting this run]\n"
        "Do NOT keep calling web_search - it will keep failing. Instead fetch a "
        "known landing page directly (e.g. https://www.mospi.gov.in/download-reports "
        "or https://data.gov.in) and read the real links out of its HTML, or "
        "answer from your own knowledge."
    )


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


def _compiles(src):
    try:
        compile(src, "<snippet>", "exec")
        return True
    except SyntaxError:
        return False


def _repair_indentation(code):
    """Models sometimes emit a stray leading space on a top-level statement.

    Try cheap rewrites, accepting one only if it actually parses - so code with
    real indentation (loops, defs) is never silently mangled.
    """
    if _compiles(code):
        return code
    for candidate in (
        textwrap.dedent(code),
        "\n".join(line.lstrip() for line in code.splitlines()),
    ):
        if _compiles(candidate):
            return candidate
    return code


def _save_tool_output(workdir, tool, text):
    """Persist a tool result so run_python can load it instead of retyping it."""
    name = f"{tool}_{len(os.listdir(workdir)) + 1}.json"
    try:
        with open(os.path.join(workdir, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return text
    return (
        f"[full result saved to {name} in the working directory - load it in "
        f"run_python (e.g. json.load(open('{name}'))) instead of retyping the "
        f"data]\n{_truncate(text)}"
    )


def run_python(code, workdir):
    """Run code in a subprocess. Never raises; returns stdout/stderr as text."""
    script = os.path.join(workdir, "_snippet.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(PREAMBLE + "\n" + _repair_indentation(code))
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


MOSPI_MCP_URL = os.environ.get("MOSPI_MCP_URL", "https://mcp.mospi.gov.in/mcp")
MCP_PREFIX = "mospi_"
_MCP_CACHE = {}


def _mcp_rpc(method, params=None, timeout=90):
    """One JSON-RPC call to the MoSPI MCP server. Replies are SSE-framed."""
    resp = requests.post(
        MOSPI_MCP_URL,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        timeout=timeout,
    )
    resp.raise_for_status()
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(resp.text)


def mospi_tools():
    """The MCP server's own tools, as OpenAI function definitions.

    Fetched once and cached. If the server is unreachable the agent simply runs
    without them rather than failing the whole question.
    """
    if "tools" not in _MCP_CACHE:
        try:
            listed = _mcp_rpc("tools/list", timeout=30)["result"]["tools"]
            _MCP_CACHE["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": MCP_PREFIX + t["name"],
                        "description": (t.get("description") or "")[:1000],
                        "parameters": t.get("inputSchema")
                        or {"type": "object", "properties": {}},
                    },
                }
                for t in listed
            ]
        except Exception:  # noqa: BLE001 - optional capability
            _MCP_CACHE["tools"] = []
    return _MCP_CACHE["tools"]


MAX_MCP_PAGES = 25


def _mcp_once(name, args):
    payload = _mcp_rpc(
        "tools/call",
        {"name": name[len(MCP_PREFIX):], "arguments": args or {}},
    )
    if "error" in payload:
        return f"[MCP error] {payload['error']}"
    content = payload.get("result", {}).get("content", [])
    text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
    return text or json.dumps(payload.get("result", {}))


def mospi_call(name, args):
    """Invoke one MCP tool, following pagination to completion.

    get_data returns 10 rows per page. Handing the model only page 1 makes any
    max/min/sum over the result silently wrong, so every page is fetched and
    merged before the result is returned.
    """
    text = _mcp_once(name, args)
    try:
        first = json.loads(text)
    except (ValueError, TypeError):
        return text

    meta = first.get("meta_data") if isinstance(first, dict) else None
    rows = first.get("data") if isinstance(first, dict) else None
    if not (isinstance(meta, dict) and isinstance(rows, list)):
        return text

    total_pages = int(meta.get("totalPages") or 1)
    if total_pages <= 1:
        return text

    merged = list(rows)
    for page in range(2, min(total_pages, MAX_MCP_PAGES) + 1):
        paged = dict(args or {})
        paged["filters"] = {**(paged.get("filters") or {}), "page": page}
        try:
            more = json.loads(_mcp_once(name, paged))
        except (ValueError, TypeError):
            break
        chunk = more.get("data") if isinstance(more, dict) else None
        if not chunk:
            break
        merged.extend(chunk)

    first["data"] = merged
    first["meta_data"] = {
        **meta,
        "page": "all",
        "pages_fetched": min(total_pages, MAX_MCP_PAGES),
        "rows_returned": len(merged),
    }
    # Full text on purpose - the caller saves it to disk and truncates only the
    # copy shown to the model.
    return json.dumps(first)


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
        base_url=BASE_URL,
        api_key=os.environ["LLM_API_KEY"],
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
    tools = TOOLS + mospi_tools()
    logger.log("tools_available", tools=[t["function"]["name"] for t in tools])
    answer = None

    for step in range(MAX_STEPS):
        logger.log("model_request", iteration=step, message_count=len(messages))
        try:
            resp = client.chat.completions.create(
                model=MODEL, messages=messages, tools=tools, temperature=0
            )
        except Exception as exc:  # noqa: BLE001
            logger.log("model_error", iteration=step, error=str(exc))
            answer = f"[error contacting model: {exc}]"
            break

        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []
        logger.log(
            "model_response",
            iteration=step,
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
            logger.log("tool_call", iteration=step, tool=name, arguments=args)
            try:
                if name.startswith(MCP_PREFIX):
                    result = _save_tool_output(workdir, name, mospi_call(name, args))
                elif name == "web_search":
                    result = web_search(args.get("query", ""))
                elif name == "fetch_url":
                    result = fetch_url(args.get("url", ""), workdir)
                elif name == "run_python":
                    result = run_python(args.get("code", ""), workdir)
                else:
                    result = f"[ERROR] Unknown tool: {name}"
            except Exception as exc:  # noqa: BLE001
                result = f"[ERROR] {type(exc).__name__}: {exc}"
            logger.log("tool_result", iteration=step, tool=name, result=result)
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result}
            )
    else:
        # Step budget exhausted. Force a shaped answer with one tool-free call
        # rather than returning an error string the grader can never match.
        logger.log("step_limit_reached", note="forcing final answer without tools")
        messages.append(
            {
                "role": "user",
                "content": (
                    "You are out of tool-calling steps. Answer now using what you "
                    "already gathered, falling back on your own knowledge where "
                    "you must. Reply with ONLY the JSON value in the exact shape "
                    "the original question asked for — no prose, no apology."
                ),
            }
        )
        try:
            resp = client.chat.completions.create(
                model=MODEL, messages=messages, temperature=0
            )
            answer = _parse_answer(resp.choices[0].message.content or "")
            logger.log("final_answer", answer=answer, note="forced after step limit")
        except Exception as exc:  # noqa: BLE001
            answer = f"[error: {exc}]"
            logger.log("final_answer", answer=answer, note="forced call failed")

    return answer, logger
