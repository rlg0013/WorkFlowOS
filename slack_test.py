#!/usr/bin/env python3
"""Post a uniquely-tagged test message to Slack and report exactly what happened.

    python slack_test.py

Prints the HTTP status and the raw response body from Slack, so "did it actually
post?" is answered by Slack rather than guessed at. The message contains a random
token you can search for in Slack to locate the channel.
"""
import os
import re
import secrets
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "python"))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"), override=False, encoding="utf-8-sig")
except ImportError:
    pass

from executor.executor import SlackClient  # noqa: E402


def main():
    url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        print("  [!!] SLACK_WEBHOOK_URL is not set in .env")
        print("      Create one: api.slack.com/apps -> your app -> Incoming Webhooks")
        return 1

    parsed = urllib.parse.urlparse(url)
    # Path is /services/<team>/<channel>/<secret> - "services" is a literal
    # segment, so the ids are at index 1 and 2, not 0 and 1.
    segments = [p for p in parsed.path.split("/") if p]
    match = re.search(r"/services/(T[A-Z0-9]+)/(B[A-Z0-9]+)/([A-Za-z0-9]+)", parsed.path)
    print("\n  WEBHOOK IDENTITY")
    print(f"    host      : {parsed.hostname}")
    if match:
        team, channel, secret = match.groups()
        print(f"    workspace : {team}")
        print(f"    channel id: {channel}")
        print(f"    secret    : {secret[:4]}...{secret[-2:]} (not shown)")
    else:
        print(f"    path parts: {segments}  (unexpected shape)")
    print(f"    full url  : {len(url)} chars")

    if not re.match(r"^https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+$", url):
        print("\n  [!!] This is not a valid incoming-webhook URL shape.")
        print("      Expected: https://hooks.slack.com/services/T…/B…/…")
        return 1

    token = secrets.token_hex(4).upper()
    text = f"WorkFlowOS Slack connectivity test - token {token}"
    print(f"\n  POSTING\n    {text}\n")

    client = SlackClient(url)
    ok = client.send_notification(text)

    status, body = client.last_response or (None, "(no response captured)")
    print("  SLACK'S ACTUAL RESPONSE")
    print(f"    status : {status}")
    print(f"    body   : {body}")

    print(f"\n  RESULT: {'Slack accepted the message' if ok else 'Slack did NOT accept it'}")

    if ok:
        print(f"\n  Now search Slack for:  {token}")
        print("  The message is in the channel bound to this webhook.")
        print("  If you cannot find it:")
        print("   - check you are in the right WORKSPACE (the webhook is bound to one)")
        print("   - Slack search can lag a minute; look in the channel directly")
        print("   - the channel may be one you are not a member of; open it via link")
    else:
        print("\n  The webhook was rejected, so nothing was posted anywhere.")
        print("  Re-create it: api.slack.com/apps -> your app -> Incoming Webhooks")
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
