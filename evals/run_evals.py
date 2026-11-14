"""ServiceLens eval suite.

Asks real questions through Claude Code (which calls your MCP server) and
checks each answer for expected facts. The unit tests prove the code is
correct; these evals prove an agent can use the tools to answer correctly.

Usage (from the project root, venv active):
  python evals/run_evals.py              run every case
  python evals/run_evals.py --only 3     run just case number 3
  python evals/run_evals.py --list       show the cases without running

Each case costs one Claude Code request on your account.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES_FILE = Path(__file__).resolve().parent / "cases.json"
RESULTS_FILE = Path(__file__).resolve().parent / "last_run.json"
SERVER_NAME = "servicelens"
TIMEOUT_SECONDS = 180


def load_cases() -> list[dict]:
    return json.loads(CASES_FILE.read_text(encoding="utf-8"))


def ask(claude: str, question: str) -> str:
    """Run one question through Claude Code and return the answer text."""
    cmd = [
        claude,
        "-p",
        question,
        "--output-format",
        "json",
        "--max-turns",
        "10",
        "--allowedTools",
        f"mcp__{SERVER_NAME}",  # let the agent call this server's tools unprompted
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        timeout=TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exited with code {proc.returncode}: {proc.stderr.strip()[:300]}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"could not parse claude output: {proc.stdout[:300]}")
    if data.get("is_error"):
        raise RuntimeError(f"claude reported an error: {str(data.get('result'))[:300]}")
    return str(data.get("result", ""))


def grade(case: dict, answer: str) -> list[str]:
    """Return a list of problems; an empty list means the case passed."""
    text = answer.lower()
    problems = []
    for needle in case.get("must_include", []):
        if needle.lower() not in text:
            problems.append(f"missing '{needle}'")
    any_of = case.get("must_include_any")
    if any_of and not any(n.lower() in text for n in any_of):
        problems.append("expected at least one of: " + ", ".join(any_of))
    for needle in case.get("must_not_include", []):
        if needle.lower() in text:
            problems.append(f"should not contain '{needle}'")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the ServiceLens evals.")
    parser.add_argument("--only", type=int, help="run only this case number (1-based)")
    parser.add_argument("--list", action="store_true", help="list cases and exit")
    args = parser.parse_args()

    cases = load_cases()
    if args.list:
        for i, c in enumerate(cases, 1):
            print(f"{i:2}. [{c['id']}] {c['question']}")
        return 0

    if args.only:
        if not 1 <= args.only <= len(cases):
            print(f"--only must be between 1 and {len(cases)}")
            return 2
        selected = [(args.only, cases[args.only - 1])]
    else:
        selected = list(enumerate(cases, 1))

    claude = shutil.which("claude")
    if not claude:
        print("Could not find the 'claude' command. Run 'claude --version' first.")
        return 2

    results, passed = [], 0
    for number, case in selected:
        print(f"[{number}/{len(cases)}] {case['id']}: {case['question']}")
        started = time.time()
        try:
            answer = ask(claude, case["question"])
            problems = grade(case, answer)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            answer, problems = "", [f"run failed: {e}"]
        seconds = round(time.time() - started, 1)
        ok = not problems
        passed += ok
        print(f"      {'PASS' if ok else 'FAIL'} ({seconds}s)")
        for p in problems:
            print(f"        - {p}")
        results.append(
            {
                "id": case["id"],
                "question": case["question"],
                "passed": ok,
                "problems": problems,
                "seconds": seconds,
                "answer": answer,
            }
        )

    total = len(selected)
    print(f"\n{passed}/{total} passed ({round(100 * passed / total)}%)")
    RESULTS_FILE.write_text(
        json.dumps(
            {
                "run_at": datetime.now().isoformat(timespec="seconds"),
                "passed": passed,
                "total": total,
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Details saved to {RESULTS_FILE.relative_to(ROOT)}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())