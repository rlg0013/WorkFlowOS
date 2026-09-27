#!/usr/bin/env python3
"""Print the evidence trail of the most recent WorkFlowOS run.

Works on any platform with Python 3 and no extra tooling:
    python proofs.py            # latest run
    python proofs.py <id>       # a specific workflow
"""
import json
import sys
import urllib.request

BASE = "http://localhost:8080/api/v1"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.load(response)


def main():
    try:
        workflows = get("/workflows").get("workflows") or []
    except Exception as exc:
        print(f"Cannot reach the WorkFlowOS backend on port 8080 ({exc}).")
        print("Start it with:  cd go && go run cmd/server/main.go")
        return 1

    if not workflows:
        print("No workflows recorded yet.")
        print("Run the demo at http://localhost:8080, or:  python test_e2e.py")
        return 1

    workflow_id = sys.argv[1] if len(sys.argv) > 1 else workflows[0]["id"]
    run = get(f"/workflows/{workflow_id}/proofs")

    print("=" * 62)
    print(f"intent    : {run['intent']}")
    print(f"workflow  : {run['workflow_id']}")
    print(f"status    : {run['status']}")
    print(f"steps     : {run['steps_ok']}/{run['steps_total']} ok, {run['steps_failed']} failed")
    if run.get("executed_at"):
        print(f"executed  : {run['executed_at']}")
    print("=" * 62)

    print("\nPER-STEP TRACE")
    for step in get(f"/workflows/{workflow_id}/steps")["steps"]:
        mark = {"ok": "[ok]", "failed": "[!!]", "running": "[..]"}.get(step["status"], "[??]")
        print(f"  {mark} step {step['step']}  {step['duration_ms']:>5}ms  {step['summary']}")

    print("\nPROOF ARTIFACTS")
    proofs = run["proofs"]
    if not proofs:
        print("  (none recorded)")
    for proof in proofs:
        state = "VERIFIED" if proof["verified"] else "INCOMPLETE"
        print(f"  [{state}] {proof['kind']:<13} {proof['label']}")
        print(f"             {proof['detail']}")
        if proof.get("href"):
            print(f"             download: http://localhost:8080{proof['href']}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
