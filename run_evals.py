"""Score this bot against the course grading pipeline, without Telegram.

`collect.py` in the pipeline repo is the only stage that touches Telegram, and
it needs a logged-in user account plus a running bot. This replaces just that
stage: questions are rendered exactly as collect.py renders them, the agent is
called directly with the same per-chat history main.py keeps, and results are
written in collect.py's format so the pipeline's own grade.py scores them
unmodified.

    git clone https://github.com/Jivraj-18/tds-p1-t2-2026-telegram-bot ../pipeline
    python run_evals.py --pipeline ../pipeline

The bot replies with {"answer": ..., "log_url": ...}; the reference grade.py
compares the whole reply, so `answer` is unwrapped here to match the real
grader's semantics.
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from string import Template

from dotenv import load_dotenv

load_dotenv()

from agent import answer_question  # noqa: E402


def slugify(email):
    return re.sub(r"[^a-zA-Z0-9]+", "_", email.strip().lower()).strip("_")


def render(text, vars_):
    return Template(text).substitute({k: json.dumps(v) for k, v in vars_.items()})


def run_one(pipeline, inputs, question, email):
    qid = question["id"]
    vars_ = inputs[qid][email]
    messages = [render(t, vars_) for t in question["messages"]]

    history, replies = [], []
    try:
        for msg in messages:
            history.append({"role": "user", "content": msg})
            answer, _logger = answer_question(history, chat_id=slugify(email))
            envelope = json.dumps(
                {"answer": answer, "log_url": "https://example.invalid/run.jsonl"},
                ensure_ascii=False,
                default=str,
            )
            history.append({"role": "assistant", "content": envelope})
            replies.append(json.dumps(answer, ensure_ascii=False, default=str))
        status = "ok"
    except Exception as exc:  # noqa: BLE001 - record and keep scoring the rest
        status, replies = "error", replies + [f"[{type(exc).__name__}: {exc}]"]

    out_dir = Path(pipeline) / "data" / slugify(email)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{qid}.json").write_text(
        json.dumps(
            {"status": status, "vars": vars_, "sent": messages, "replies": replies},
            indent=2,
        )
    )
    print(f"  {qid}: {status} -> {replies[-1][:70]}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pipeline", required=True, help="clone of the grading pipeline")
    ap.add_argument("--students", default="students.csv", help="roster, relative to --pipeline")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    pipeline = os.path.abspath(args.pipeline)
    roster = os.path.join(pipeline, args.students)

    subprocess.run(
        [sys.executable, "generate.py", "--students", args.students],
        cwd=pipeline, check=True,
    )

    questions = json.load(open(f"{pipeline}/evals/questions.json"))
    inputs = json.load(open(f"{pipeline}/inputs.json"))
    students = list(csv.DictReader(open(roster, newline="")))

    jobs = [(q, s["email"]) for q in questions for s in students]
    print(f"\nrunning {len(jobs)} questions through the agent...\n", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(lambda j: run_one(pipeline, inputs, *j), jobs))

    print()
    subprocess.run(
        [sys.executable, "grade.py", "--students", args.students],
        cwd=pipeline, check=True,
    )


if __name__ == "__main__":
    main()
