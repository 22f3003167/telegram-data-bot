"""Inspect a run log and show where the step budget went.

The bot's reply contains a log_url. Feed it here to see the whole run - every
model turn, tool call, tool result, and any failures - plus a summary that
flags the usual budget wasters: repeated identical calls, truncated results,
and crashed run_python snippets.

    python inspect_log.py <log_url|local.jsonl>
    python inspect_log.py <log_url> --full     # don't abbreviate payloads
"""

import argparse
import collections
import json
import sys

import requests


def load(source):
    if source.startswith(("http://", "https://")):
        resp = requests.get(source, timeout=60)
        resp.raise_for_status()
        text = resp.text
    else:
        text = open(source, encoding="utf-8").read()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="log_url from the bot's reply, or a local .jsonl")
    ap.add_argument("--full", action="store_true", help="show untruncated payloads")
    args = ap.parse_args()

    records = load(args.source)
    cut = 100000 if args.full else 200

    calls = collections.Counter()
    truncated = []
    failures = []

    print("=" * 72)
    for rec in records:
        step = rec["step"]
        if step == "user_question":
            print(f"QUESTION: {rec['content'][:cut]}")
        elif step == "tools_available":
            print(f"TOOLS   : {len(rec['tools'])} available")
        elif step == "tool_call":
            sig = (rec["tool"], json.dumps(rec["arguments"], sort_keys=True))
            calls[sig] += 1
            print(f"\n  CALL  {rec['tool']}  {json.dumps(rec['arguments'])[:cut]}")
        elif step == "tool_result":
            body = str(rec["result"])
            note = ""
            if "[truncated," in body:
                truncated.append(rec["tool"])
                note = "  <-- TRUNCATED, data lost"
            if body.startswith("[exit ") or body.startswith("[ERROR]"):
                failures.append((rec["tool"], body[:120]))
                note = "  <-- FAILED"
            print(f"  ->    {len(body)} chars{note}")
            print(f"        {body[:cut]}")
        elif step == "model_response" and rec.get("content"):
            print(f"\n  MODEL: {str(rec['content'])[:cut]}")
        elif step == "step_limit_reached":
            print("\n  !! STEP LIMIT REACHED - answer was forced without tools")
        elif step == "final_answer":
            print(f"\nANSWER  : {json.dumps(rec['answer'])[:cut]}")

    print("\n" + "=" * 72)
    print("SUMMARY")
    total = sum(calls.values())
    print(f"  tool calls: {total}")
    for (tool, argstr), n in calls.most_common():
        flag = "  <-- REPEATED IDENTICAL CALL" if n > 1 else ""
        print(f"    {n}x {tool} {argstr[:70]}{flag}")
    if truncated:
        print(f"  truncated results: {collections.Counter(truncated)}")
        print("    -> the model saw only part of these; check it used the saved file")
    if failures:
        print(f"  failed tool calls: {len(failures)}")
        for tool, msg in failures:
            print(f"    {tool}: {msg}")
    if not truncated and not failures and total == len(set(calls)):
        print("  no repeated calls, no truncation, no failures")


if __name__ == "__main__":
    sys.exit(main())
