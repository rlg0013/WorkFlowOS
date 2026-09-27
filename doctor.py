#!/usr/bin/env python3
"""WorkFlowOS live-mode preflight.

Checks every credential the real (non-demo) run needs, then lists the actual
emails in your inbox that the automation would pick up.

    python doctor.py            # check credentials only
    python doctor.py --inbox    # also list real candidate emails
    python doctor.py --slack    # also post a real test message to Slack

Nothing here is simulated. If Gmail is not authorized it says so plainly
instead of quietly falling back to demo data.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "python"))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"), override=False, encoding="utf-8-sig")
except ImportError:
    pass

from executor.executor import GmailAuthError, GmailClient, SlackClient  # noqa: E402

OK, BAD, WARN = "[ok]", "[!!]", "[--]"


def repo_path(env_name: str, default_relative: str) -> str:
    """Resolve a path setting against the repo root, not the current directory."""
    value = os.getenv(env_name, "").strip() or default_relative
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(HERE, value))


def check(label, passed, detail="", optional=False):
    mark = OK if passed else (WARN if optional else BAD)
    print(f"  {mark} {label}{(' - ' + detail) if detail else ''}")
    return passed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inbox", action="store_true", help="list real candidate emails")
    parser.add_argument("--auth", action="store_true",
                        help="authorize Gmail and exit (no need to list anything)")
    parser.add_argument("--slack", action="store_true", help="post a real test message to Slack")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    creds_path = repo_path("GMAIL_CREDENTIALS", "python/credentials.json")
    token_path = repo_path("GMAIL_TOKEN", "python/token.json")
    webhook = os.getenv("SLACK_WEBHOOK_URL", "")

    if not os.path.exists(os.path.join(HERE, ".env")) and (webhook or os.getenv("LLM_API_KEY")):
        print(f"{WARN} reading secrets from the shell environment; a repo-root .env is safer")
        print(f"    (see .env.example - it is gitignored)\n")

    print("\nGMAIL")
    has_creds = check("OAuth client credentials present", os.path.exists(creds_path), creds_path)

    if args.auth:
        if not has_creds:
            print(f"  {BAD} no OAuth client file at {creds_path} - see SETUP.md step 3")
            return 1
        client_id = ""
        try:
            client_id = json.load(open(creds_path)).get("installed", {}).get("client_id", "")
        except Exception:
            pass
        print("\n  A Google consent page will open. Two different things can appear:")
        print()
        print("   A) \"Google hasn't verified this app\" with an Advanced link")
        print("      -> click Advanced, then \"Go to WorkFlowOS (unsafe)\". Fine.")
        print()
        print("   B) \"Access blocked: ... has not completed the Google verification")
        print("      process\" and NO Advanced link")
        print("      -> your account is not a test user. There is no bypass. Fix:")
        print("         console.cloud.google.com -> project workflowos-509805")
        print("         -> Google Auth Platform -> Test users")
        print("         -> ADD USERS -> your own Gmail -> SAVE")
        print(f"         (client id {client_id[:24]}...)")
        print("      Keep publishing status = Testing. Do not request verification.\n")
        gmail = GmailClient(creds_path, token_path)
        try:
            creds = gmail.authenticate(interactive=True)
        except GmailAuthError as exc:
            if exc.reason == "not_test_user":
                print(f"  {BAD} {exc}")
            else:
                print(f"  {BAD} {exc}")
            return 1
        except Exception as exc:
            print(f"  {BAD} {type(exc).__name__}: {exc}")
            return 1
        profile = gmail.service.users().getProfile(userId='me').execute()
        print(f"\n  {OK} Authorized: {profile.get('emailAddress')}")
        print(f"  {OK} Token saved to {os.path.relpath(token_path, HERE)}")
        print(f"  {OK} Next: python doctor.py --inbox")
        return 0

    if has_creds:
        gmail = GmailClient(creds_path, token_path)
        status = gmail.auth_status()
        if status["ok"]:
            check("Authorized and working", True,
                  f"{status['email']} ({status.get('messages', '?')} messages in mailbox)")
        else:
            short = {
                "missing": "not authorized yet",
                "expired": "authorization expired",
                "unauthorized": "not authorized",
                "rejected": "Gmail rejected the token",
                "missing_credentials": "OAuth client file missing",
            }.get(status["reason"], "unavailable")
            print(f"  {WARN} Gmail: {short}")
            if status["reason"] == "expired":
                print("\n  WHY THIS IS EXPECTED")
                print("    Google expires refresh tokens after 7 days while the OAuth consent")
                print("    screen is in Testing status. Nothing is broken.")
                print("\n  FIX (about 60 seconds)")
                print("    1. Google Cloud -> APIs & Services -> OAuth consent screen")
                print("         -> Test users -> your own email must be listed -> Save")
                print("    2. Keep publishing status = Testing.")
                print("         DO NOT click 'Request verification' - it is not needed here.")
                print("    3. Delete the stale token:")
                print("         Remove-Item .\\python\\token.json")
                print("    4. Re-grant access:  python doctor.py --inbox")
            elif status["reason"] == "missing":
                print("\n  FIX:  python doctor.py --auth")
                print("    That prints a Google consent URL and waits for you to approve.")
                print("    On the warning screen click Advanced -> 'Go to WorkFlowOS (unsafe)'.")
                print("    If Google says 'Access blocked', add your own email under")
                print("    OAuth consent screen -> Test users, then Save.")
                print("    Your token is then written to python\\token.json.")
            else:
                print(f"    detail: {status['message']}")
    else:
        print("\n  See SETUP.md step 3: create an OAuth 2.0 Desktop client in Google Cloud,")
        print("  enable the Gmail API, add your own address as a test user, and save the")
        print("  downloaded JSON as python/credentials.json.")

    print("\nSLACK")
    webhook_ok = check("Incoming webhook configured", bool(webhook),
                       webhook[:38] + "..." if len(webhook) > 38 else webhook, optional=True)
    if not webhook_ok:
        print("  Create one at https://api.slack.com/apps -> From scratch -> Incoming Webhooks")
        print("  -> Add New Webhook to Workspace, then set SLACK_WEBHOOK_URL.")
    elif args.slack:
        sent = SlackClient(webhook).send_notification("WorkFlowOS live test: Slack webhook is connected.")
        check("Test message delivered", sent, "check your channel")

    print("\nLLM (optional)")
    key = os.getenv("LLM_API_KEY", "")
    if check("LLM_API_KEY set", bool(key),
             key[:12] + "..." if key else "using the local deterministic template", optional=True):
        pass
    else:
        print("  Without it, workflow generation uses the deterministic local template")
        print("  (still runtime-variable driven, so it works on real emails).")
        print("  Free keys: console.groq.com/keys or openrouter.ai/keys")

    if args.inbox and has_creds:
        print("\nREAL CANDIDATE EMAILS  (is:unread has:attachment)")
        try:
            messages = gmail.list_candidates(limit=args.limit)
        except Exception as exc:
            print(f"  {BAD} Gmail query failed: {exc}")
            return 1
        if not messages:
            print(f"  {WARN} no unread emails with attachments right now.")
            print("      Send yourself an email with a file attached (PDF/CSV/DOCX),")
            print("      leave it UNREAD, then run this again.")
            return 0
        for message in messages:
            attachments = ", ".join(message["attachments"]) or "(none listed)"
            print(f"\n  from    : {message['from']}")
            print(f"  sender  : {message['sender_email']}")
            print(f"  subject : {message['subject']}")
            print(f"  date    : {message['date']}")
            print(f"  files   : {attachments}")
            print(f"  id      : {message['message_id']}")
        print(f"\n  {len(messages)} candidate(s). Leave the one you want to process UNREAD.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
