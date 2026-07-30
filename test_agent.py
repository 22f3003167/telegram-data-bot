"""Local smoke tests for the agent loop. Run: .venv/bin/python test_agent.py"""

import json

from dotenv import load_dotenv

load_dotenv()

from agent import answer_question  # noqa: E402

CASES = [
    (
        "inline data",
        [{"role": "user", "content": (
            "Here are seven weekly sales figures: [412, 187, 903, 655, 231, 774, 508]. "
            'Reply with ONLY {"median": <number>, "max": <number>}'
        )}],
    ),
    (
        "public dataset fetch",
        [{"role": "user", "content": (
            "Fetch the CSV at https://raw.githubusercontent.com/mwaskom/seaborn-data/"
            "master/tips.csv and reply with ONLY "
            '{"rows": <row count>, "mean_tip": <mean of the tip column rounded to 2 decimals>}'
        )}],
    ),
    (
        "multi-turn",
        [
            {"role": "user", "content": (
                "I'm working with the restaurant tips dataset at "
                "https://raw.githubusercontent.com/mwaskom/seaborn-data/master/tips.csv"
            )},
            {"role": "assistant", "content": '{"answer": "Noted.", "log_url": ""}'},
            {"role": "user", "content": (
                "For that dataset, which day has the highest sum of total_bill? "
                'Reply with ONLY {"top_day": "<day>"}'
            )},
        ],
    ),
]

EXPECTED = [
    {"median": 508, "max": 903},
    {"rows": 244, "mean_tip": 3.0},
    {"top_day": "Sat"},
]

for (label, history), expected in zip(CASES, EXPECTED):
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    answer, logger = answer_question(history, chat_id="test")
    print("answer  :", json.dumps(answer))
    print("expected:", json.dumps(expected))
    print("MATCH   :", answer == expected)
    steps = [json.loads(l)["step"] for l in open(logger.path)]
    print("log     :", logger.path)
    print("steps   :", " -> ".join(steps))
