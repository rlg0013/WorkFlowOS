#!/usr/bin/env python3
"""Run WorkFlowOS end-to-end against a REAL inbox and a REAL Slack channel.

Unlike test_e2e.py (which seeds synthetic events so the demo always works), this
script uses the live path: it asks Gmail for real unread emails with attachments,
seeds the detector with the REAL sender address, and executes the approved
workflow against real Gmail and a real Slack webhook.

    python live_run.py --list                 # show real candidates, change nothing
    python live_run.py --run --slack          # full live run incl. real Slack post
    python live_run.py --run --slack --llm    # also use a real LLM to write the spec

Requires SLACK_WEBHOOK_URL for --slack. Gmail is authorized on first use via a
browser consent window.
"""
import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "python"))
sys.path.insert(0, HERE)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"), override=False, encoding="utf-8-sig")
except ImportError:
    pass

from executor.executor import GmailClient  # noqa: E402

API = "http://localhost:8080/api/v1"
OK, BAD, WARN = "[ok]", "[!!]", "[--]"


def repo_path(env_name: str, default_relative: str) -> str:
    """Resolve a path setting against the repo root, not the current directory."""
    value = os.getenv(env_name, "").strip() or default_relative
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(HERE, value))


def post(path, body):
    request = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def get(path):
    with urllib.request.urlopen(API + path, timeout=30) as response:
        return json.load(response)


def backend_up():
    try:
        get("/workflows")
        return True
    except Exception:
        return False


def show_candidates(gmail, limit):
    messages = gmail.list_candidates(limit=limit)
    if not messages:
        print(f"  {WARN} No unread emails with attachments right now.")
        print("      Send yourself an email with a file attached and LEAVE IT UNREAD.")
        return []
    for index, message in enumerate(messages, start=1):
        files = ", ".join(message["attachments"]) or "(none)"
        size = message.get("attachment_bytes") or 0
        size_text = f" ({size} bytes)" if size else ""
        print(f"\n  [{index}] from    : {message['from']}")
        print(f"      subject : {message['subject']}")
        print(f"      date    : {message['date']}")
        print(f"      files   : {files}{size_text}")
        print(f"      id      : {message['message_id']}")
    if not any(m.get("attachments") for m in messages):
        print("\n  [!!] None of these has a real attachment part.")
        print("      The automation's first step needs one. Send yourself an email")
        print("      with a file attached (PDF/CSV/DOCX) and leave it UNREAD.")
    return messages


def seed_real_events(sender, subject, session_suffix):
    """Teach the repetition detector the pattern, using the real sender.

    Repetition detection is frequency matching, not AI: the same 5-step shape has
    to be observed 3+ times. We replay it three times with today's real address
    so the detector has genuine evidence to promote.
    """
    session = f"live_{session_suffix}"
    events = [
        {"source": "gmail", "action": "open", "target": f"thread_{session[-6:]}",
         "payload": f"from:{sender}", "session_id": session},
        {"source": "gmail", "action": "download", "target": "attachment",
         "payload": subject, "session_id": session},
        {"source": "crm", "action": "lookup", "target": "customer",
         "payload": sender, "session_id": session},
        {"source": "crm", "action": "update", "target": "record",
         "payload": "request logged", "session_id": session},
        {"source": "slack", "action": "notify", "target": "ops_channel",
         "payload": "customer request", "session_id": session},
    ]
    for _ in range(3):
        for event in events:
            post("/events/ingest", event)
    return session


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="list real candidates and exit")
    parser.add_argument("--run", action="store_true", help="execute the live pipeline")
    parser.add_argument("--slack", action="store_true", help="post to the real Slack webhook")
    parser.add_argument("--first", action="store_true", help="use the newest candidate without prompting")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    creds = repo_path("GMAIL_CREDENTIALS", "python/credentials.json")
    token = repo_path("GMAIL_TOKEN", "python/token.json")
    webhook = os.getenv("SLACK_WEBHOOK_URL", "")

    print("\n=== 1. AUTHORIZE GMAIL ===")
    gmail = GmailClient(creds, token)
    try:
        gmail.authenticate()
    except Exception as exc:
        print(f"  {BAD} Authorization failed: {exc}")
        print("      A browser window should have opened. If it did not, your Google")
        print("      Cloud OAuth consent screen may be unpublished or your address may")
        print("      not be listed as a test user. See SETUP.md step 3.")
        return 1
    print(f"  {OK} Gmail authorized (token cached at {os.path.relpath(token, HERE)})")

    print("\n=== 2. REAL CANDIDATE EMAILS ===")
    try:
        messages = show_candidates(gmail, args.limit)
    except Exception as exc:
        print(f"  {BAD} {exc}")
        return 1
    if not messages:
        return 1
    if args.list or not args.run:
        print(f"\n  Re-run with --run to process the newest of these ({messages[0]['subject']!r}).")
        return 0

    chosen = messages[0]
    if len(messages) > 1 and not args.first:
        print("\n  Which one should the automation process?")
        print("    (the run marks it READ in Gmail, so pick a disposable message)")
        raw = input(f"  number [1-{len(messages)}], or Enter for 1: ").strip()
        try:
            index = int(raw) - 1 if raw else 0
            if not 0 <= index < len(messages):
                raise ValueError
        except ValueError:
            print("  [!!] invalid choice")
            return 1
        chosen = messages[index]

    sender = chosen["sender_email"]
    subject = chosen["subject"]
    files = ", ".join(chosen.get("attachments") or []) or "(none)"
    print(f"\n=== 3. TARGET SELECTED ===")
    print(f"  {OK} will process: {subject!r} from {sender}")
    print(f"      attachment: {files}")

    if not backend_up():
        print(f"\n  {BAD} Backend is not running. Start it:  cd go && go run cmd/server/main.go")
        return 1
    print(f"  {OK} backend reachable")

    # The whole point of this script is a real run. If the executor is still in
    # demo mode it will happily "succeed" while fabricating every fact, which is
    # worse than failing loudly.
    try:
        ready = json.load(urllib.request.urlopen("http://localhost:8082/readyz", timeout=10))
    except Exception as exc:
        print(f"\n  {BAD} Executor is not running ({exc}).")
        print("      Start it in a second terminal:  cd python; python -m executor.server")
        return 1
    if ready.get("mode") == "demo":
        print("\n  [!!] The executor is in DEMO mode - this run would be entirely simulated.")
        print("      Gmail and Slack would be faked, which defeats the purpose.")
        print("\n      Stop the executor (Ctrl+C) and restart it with demo mode off:")
        print("        cd python")
        print("        $env:DEMO_MODE='false'")
        print("        python -m executor.server")
        print('\n      Check with:  curl.exe -s http://localhost:8082/readyz')
        print('      It must report "mode": "live" before continuing.')
        return 1
    missing = ready.get("missing") or []
    if missing:
        print(f"\n  {BAD} Executor reports missing configuration: {', '.join(missing)}")
        print("      Fix these in .env, then restart the executor.")
        return 1
    print(f"  {OK} executor is LIVE (mode={ready.get('mode')})")

    print("\n=== 4. REPETITION DETECTION (real sender) ===")
    import time
    session = seed_real_events(sender, subject, chosen["message_id"][-6:])
    time.sleep(3)
    # Scope to the session we just seeded, otherwise a stale pattern promoted by
    # an earlier test run gets adopted and the detection step proves nothing.
    sequences = get(f"/sequences?session_id={session}").get("sequences") or []
    match = next((s for s in sequences if s["count"] >= 3), None)
    if not match:
        print(f"  {BAD} detector did not promote a candidate for this session.")
        print(f"      sequences in session {session}: {len(sequences)}")
        print("      Not falling back to an unscoped list on purpose - that would")
        print("      silently reuse another run's data and fake the detection step.")
        return 1
    print(f"  {OK} candidate promoted from session {session}")
    print(f"      pattern: {match['pattern']}")
    print(f"      observed {match['count']} times")

    print("\n=== 5. GENERATE WORKFLOW ===")
    created = post("/workflows/generate", {"sequence_id": match["id"]})
    spec = created["spec"]
    workflow_id = created["workflow"]["id"]
    print(f"  {OK} intent: {spec['intent']}")
    for action in spec["actions"]:
        print(f"      {action['step']}. {action['action']:<24} {json.dumps(action.get('params', {}))}")

    print("\n=== 6. APPROVE (the human gate) ===")
    post("/workflows/approve", {"workflow_id": workflow_id, "approved": True})
    print(f"  {OK} approved {workflow_id[:8]}")

    print("\n=== 7. EXECUTE FOR REAL ===")
    result = post("/workflows/execute", json_body := {"workflow_id": workflow_id})
    steps = result.get("steps", [])
    for step in steps:
        mark = OK if step["status"] == "ok" else BAD
        print(f"  {mark} step {step['step']} {step['duration_ms']:>5}ms  {step['summary']}")

    # Verify the run was genuinely real before claiming anything about Slack.
    # Checking that a webhook is configured proves nothing about what ran.
    slack_step = next((s for s in steps if s["action"] == "send_slack_notification"), None)
    slack_simulated = bool(slack_step and ("[DEMO]" in slack_step["summary"]
                                           or "dry-run" in slack_step["summary"]))
    gmail_step = next((s for s in steps if s["action"] == "download_attachment"), None)
    gmail_simulated = bool(gmail_step and "[DEMO]" in gmail_step["summary"])

    print("\n=== 8. PROOFS ===")
    proofs = get(f"/workflows/{workflow_id}/proofs")
    for proof in proofs["proofs"]:
        state = "VERIFIED" if proof["verified"] else "INCOMPLETE"
        print(f"  [{state}] {proof['kind']:<13} {proof['label']}")
        print(f"             {proof['detail']}")
        if proof.get("href"):
            print(f"             http://localhost:8080{proof['href']}")

    print("\n=== 9. REALITY CHECK ===")
    if gmail_simulated:
        print(f"  {BAD} Gmail step was SIMULATED - the attachment is not real.")
    else:
        print(f"  {OK} Gmail step hit the real API.")
    if slack_simulated:
        print(f"  {BAD} Slack step was SIMULATED - nothing was posted to Slack.")
    elif slack_step and slack_step["status"] == "ok":
        print(f"  {OK} Slack webhook accepted the message.")
    elif slack_step:
        print(f"  {BAD} Slack step failed: {slack_step['summary']}")

    if not webhook:
        print(f"\n  {WARN} SLACK_WEBHOOK_URL is not set in .env, so no channel is")
        print("      configured. An incoming webhook posts to whichever channel it was")
        print("      bound to when you created it - there is no WorkFlowOS DM.")
        print("      To find it: api.slack.com/apps -> your app -> Incoming Webhooks")
        print("      -> the webhook URL ends with T…/B…/… ; the B… is the channel id.")
    elif slack_simulated:
        print("\n      Re-run with the executor restarted in live mode to actually post.")
    else:
        print("\n      Open the channel bound to your webhook to see the message.")

    print(f"\n  Full trace: python proofs.py {workflow_id}\n")
    return 0 if not (gmail_simulated or slack_simulated) else 1


if __name__ == "__main__":
    sys.exit(main())
