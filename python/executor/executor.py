import json
import os
import base64
import logging
import atexit
import re
import time
import requests
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from playwright.sync_api import sync_playwright
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from shared.events import WorkflowSpec


class GmailAuthError(RuntimeError):
    """Authorization is missing or stale and cannot be fixed without a human."""

    def __init__(self, message: str, reason: str = "unauthorized"):
        super().__init__(message)
        self.reason = reason


CONSENT_HEADING = "A Google consent window is opening in your browser."
CONSENT_STEPS = [
    "1. Sign in to the Gmail account you want to automate.",
    "2. You may see \"Google hasn't verified this app\" - that is EXPECTED.",
    "   Click Advanced, then \"Go to WorkFlowOS (unsafe)\". No review is needed",
    "   for your own account while the consent screen stays in Testing mode.",
    "3. If Google says \"Access blocked\", your account is not a test user:",
    "   Google Cloud -> APIs & Services -> OAuth consent screen -> Test users",
    "   -> ADD YOUR OWN EMAIL, then Save.",
]

AUTH_HEADING = "STEP 1 of 2 - approve access in your browser"
AUTH_WAIT_HEADING = "STEP 2 of 2 - waiting for you to approve"
AUTH_TIMEOUT_SECONDS = 300


def _wrap_url(url: str, width: int = 76) -> List[str]:
    """Break a long OAuth URL so it stays clickable/copyable in a terminal."""
    return [url[i:i + width] for i in range(0, len(url), width)]


TOKEN_STALE_HEADING = "Your saved Google token expired and cannot be refreshed."
TOKEN_STALE_STEPS = [
    "While the OAuth consent screen is in Testing status, Google expires refresh",
    "tokens after 7 days. Re-granting access takes one click:",
    "",
    "  Google Cloud -> APIs & Services -> OAuth consent screen",
    "    -> Test users -> confirm your email is listed -> Save",
    "    -> Publish/Submit (keep status = Testing, do not request verification)",
    "",
    "Then delete the stale token and re-run this command:",
]
TOKEN_STALE_MESSAGE = (
    "Gmail authorization has expired. While the OAuth consent screen is in Testing "
    "status Google expires refresh tokens after 7 days. Delete python/token.json, "
    "confirm your Google account is listed under OAuth consent screen -> Test users, "
    "then run 'python doctor.py --inbox' to grant access again. You do NOT need to "
    "apply for Google app verification."
)


SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 'https://www.googleapis.com/auth/gmail.modify']
logger = logging.getLogger("workflowos.executor")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def extract_email(value: str) -> str:
    match = EMAIL_RE.search(value or "")
    return match.group(0).lower() if match else ""


def resolve(value, context: Dict[str, Any]):
    """Substitute {{token}} placeholders from the runtime context.

    This is what makes an automation real rather than a recording of one run: the
    spec says "the sender of the email we just processed", not a baked-in address.
    """
    if isinstance(value, str):
        def sub(match):
            key = match.group(1)
            if key in context and context[key] not in (None, ""):
                return str(context[key])
            return match.group(0)
        return TOKEN_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: resolve(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, context) for v in value]
    return value


def unresolved_tokens(value) -> List[str]:
    if isinstance(value, str):
        return TOKEN_RE.findall(value)
    if isinstance(value, dict):
        return [t for v in value.values() for t in unresolved_tokens(v)]
    if isinstance(value, list):
        return [t for v in value for t in unresolved_tokens(v)]
    return []


@dataclass
class ExecutionResult:
    success: bool
    message: str
    outputs: Dict[str, Any]
    steps: List[Dict[str, Any]] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)


class BrowserPool:
    """Lazy one-browser pool for sequential local workflow execution."""
    def __init__(self):
        self._playwright = None
        self._browser = None

    def page(self):
        if self._browser is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=os.getenv("BROWSER_HEADLESS", "true").lower() == "true")
        return self._browser.new_page()

    def close(self):
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str, auth_timeout: int = 0):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = None
        self.auth_timeout = auth_timeout or AUTH_TIMEOUT_SECONDS

    def authenticate(self, interactive: bool = True):
        creds = None
        if os.path.exists(self.token_file):
            try:
                creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)
            except (ValueError, KeyError) as exc:
                logger.warning("token file unreadable, re-authorizing: %s", exc)
                creds = None

        if creds and not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError as exc:
                    # Google caps refresh tokens at 7 days while the OAuth consent
                    # screen is in "Testing" status, and revokes them if the app is
                    # unpublished. Fall back to interactive re-consent.
                    logger.info("stored_gmail_token_stale", extra={"error": str(exc)})
                    if not interactive:
                        raise GmailAuthError(TOKEN_STALE_MESSAGE, reason="expired") from exc
                    print(f"\n  !! {TOKEN_STALE_HEADING}")
                    for line in TOKEN_STALE_STEPS:
                        print(f"     {line}")
                    print()
                    creds = None
            if creds is None or not creds.valid:
                if not interactive:
                    raise GmailAuthError(
                        "Gmail is not authorized yet. Run 'python doctor.py --inbox' once "
                        "to grant access in a browser.", reason="missing")
                creds = self._interactive_consent()

        if creds is None:
            if not interactive:
                raise GmailAuthError(
                    "Gmail is not authorized yet. Run 'python doctor.py --inbox' once "
                    "to grant access in a browser.", reason="missing")
            creds = self._interactive_consent()

        with open(self.token_file, 'w', encoding='utf-8') as token:
            token.write(creds.to_json())
        self.service = build('gmail', 'v1', credentials=creds)
        return creds

    def _interactive_consent(self) -> Credentials:
        """Consent flow we fully control.

        google-auth's run_local_server() prints its authorization URL into the
        middle of library output and depends on the OS opening a browser. This
        prints a clearly labelled URL first, attempts to launch a browser, and
        works over plain copy-paste too - which is what you need when auto-launch
        fails or you are on a headless box.
        """
        import socket
        import socketserver
        import threading
        import webbrowser
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import parse_qs, urlparse

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        # "localhost" resolves to ::1 on many Windows machines. An IPv4-only
        # listener would leave the browser redirect hanging, so accept both.
        redirect_uri = f"http://localhost:{port}"
        captured: Dict[str, object] = {}

        # Build the flow before the server so the handler can exchange the code.
        # redirect_uri belongs on the Flow constructor: OAuth2Session already
        # holds it, so re-passing it to authorization_url() is a duplicate arg.
        flow = InstalledAppFlow.from_client_secrets_file(
            self.credentials_file, SCOPES, redirect_uri=redirect_uri)
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
                # Browsers hit /favicon.ico on the same origin right after the
                # callback. That is NOT an OAuth response and must not be allowed
                # to overwrite a successful authorization with error="unknown".
                if urlparse(self.path).path not in ("", "/"):
                    self._respond(404, "<p>Not the OAuth callback. Ignore.</p>")
                    return
                if any(key in captured for key in ("credentials", "exchange_error", "error")):
                    self._respond(200, "<h2>Already authorized.</h2><p>You can close this tab.</p>")
                    return

                query = parse_qs(urlparse(self.path).query)
                if "code" not in query:
                    captured["error"] = query.get("error", ["unknown"])[0]
                    self._respond(400, f"<h2>Authorization failed</h2><p>{captured['error']}</p>")
                    return
                # Exchange the code HERE, before replying, so the browser page
                # reports the real outcome. Telling the user "authorized" before
                # the exchange happens is how a failure gets silently missed.
                try:
                    flow.fetch_token(code=query["code"][0])
                    captured["credentials"] = flow.credentials
                except Exception as exc:
                    captured["exchange_error"] = f"{type(exc).__name__}: {exc}"
                    self._respond(400, "<h2>Token exchange failed</h2>"
                                       "<p>See the terminal for details, then run this"
                                       " command again.</p>")
                    return
                self._respond(200, "<h2>WorkFlowOS is authorized.</h2>"
                                   "<p>Your token was saved. Close this tab and return"
                                   " to the terminal.</p>")

            def _respond(self, status: int, message: str):
                payload = (f"<html><body style='font:16px monospace;padding:40px'>"
                           f"{message}</body></html>").encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass  # keep terminal output clean

        class DualStackServer(socketserver.ThreadingTCPServer):
            address_family = socket.AF_INET6
            daemon_threads = True
            allow_reuse_address = True

            def server_bind(self):
                try:
                    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except (AttributeError, OSError):
                    pass
                return super().server_bind()

        try:
            server = DualStackServer(("::", port), Handler)
        except OSError:
            server = HTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        print(f"\n  {AUTH_HEADING}")
        print(f"    listening on {redirect_uri}")
        print("\n  If no browser opened, paste this URL into any browser:\n")
        for chunk in _wrap_url(auth_url):
            print(f"    {chunk}")
        print("\n  Sign in, then on the warning screen click")
        print("  Advanced -> \"Go to WorkFlowOS (unsafe)\" to continue.\n")
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

        print(f"  {AUTH_WAIT_HEADING} (Ctrl+C to cancel)")
        deadline = time.time() + self.auth_timeout
        settled = ("credentials", "exchange_error", "error")
        try:
            while not any(key in captured for key in settled):
                if time.time() > deadline:
                    raise GmailAuthError(
                        f"No response within {self.auth_timeout // 60} minutes. Open the URL "
                        "above, approve access, then run this command again.", reason="timeout")
                time.sleep(0.25)
        finally:
            server.shutdown()
            server.server_close()

        # Success wins. A stray request (favicon, prefetch) must never turn a
        # completed authorization into a failure.
        if "credentials" in captured:
            return captured["credentials"]  # type: ignore[return-value]
        if "exchange_error" in captured:
            raise GmailAuthError(
                f"Google approved the request but the token exchange failed: "
                f"{captured['exchange_error']}", reason="exchange_failed")
        if "error" in captured:
            detail = str(captured["error"])
            if detail in ("access_denied", "403"):
                raise GmailAuthError(
                    "Google blocked this account. This is NOT the 'app not verified' "
                    "warning (that one has an Advanced link you can click through). "
                    "This hard block has no bypass and means YOUR ACCOUNT IS NOT LISTED "
                    "AS A TEST USER. Fix it in Google Cloud Console: OAuth consent "
                    "screen -> Test users -> ADD USERS -> enter your own Gmail address -> "
                    "SAVE. Then run this command again. Keep publishing status = Testing "
                    "and do not request verification.", reason="not_test_user")
            raise GmailAuthError(
                f"Google returned '{detail}' during authorization.", reason="denied")
        raise GmailAuthError("Authorization ended unexpectedly.", reason="unknown")

    def auth_status(self) -> Dict[str, Any]:
        """Actually exercise the token so we report fact, not file presence."""
        if not os.path.exists(self.token_file):
            return {"ok": False, "reason": "missing",
                    "message": "not authorized yet - a browser consent window is needed once"}
        if not os.path.exists(self.credentials_file):
            return {"ok": False, "reason": "missing_credentials",
                    "message": f"OAuth client file not found: {self.credentials_file}"}
        try:
            self.authenticate(interactive=False)
        except GmailAuthError as exc:
            return {"ok": False, "reason": exc.reason, "message": str(exc)}
        except RefreshError:
            return {"ok": False, "reason": "expired", "message": TOKEN_STALE_MESSAGE}
        except Exception as exc:
            return {"ok": False, "reason": "error", "message": f"{type(exc).__name__}: {exc}"}

        try:
            profile = self.service.users().getProfile(userId='me').execute()
        except HttpError as exc:
            return {"ok": False, "reason": "rejected", "message": f"Gmail rejected the token: {exc}"}
        except RefreshError:
            return {"ok": False, "reason": "expired", "message": TOKEN_STALE_MESSAGE}
        return {"ok": True, "reason": "ok",
                "email": profile.get("emailAddress", ""),
                "messages": profile.get("messagesTotal"),
                "message": "authorized"}

    def get_latest_unread(self, query: str = "is:unread") -> Optional[Dict]:
        if self.service is None:
            self.authenticate(interactive=True)
        try:
            results = self.service.users().messages().list(userId='me', q=query, maxResults=1).execute()
            messages = results.get('messages', [])
            if not messages:
                return None
            msg = self.service.users().messages().get(userId='me', id=messages[0]['id'], format='full').execute()
            return msg
        except HttpError as e:
            print(f"Gmail API error: {e}")
            return None

    def list_candidates(self, query: str = "is:unread has:attachment", limit: int = 5) -> List[Dict]:
        """Real, human-readable view of the emails this automation could process."""
        if self.service is None:
            self.authenticate(interactive=True)
        try:
            results = self.service.users().messages().list(userId='me', q=query, maxResults=limit).execute()
        except HttpError as exc:
            raise RuntimeError(f"Gmail API error while listing candidates: {exc}") from exc

        out = []
        for stub in results.get('messages', []):
            try:
                # format='full' is required: 'metadata' omits the MIME parts tree,
                # which makes every message look like it has no attachments.
                full = self.service.users().messages().get(
                    userId='me', id=stub['id'], format='full').execute()
            except HttpError:
                continue
            headers = {h['name']: h['value'] for h in full.get('payload', {}).get('headers', [])}
            names = []
            total = 0

            def walk(part):
                nonlocal total
                if part.get('parts'):
                    for child in part['parts']:
                        walk(child)
                    return
                if part.get('filename'):
                    names.append(part['filename'])
                    total += part.get('body', {}).get('size', 0) or 0

            walk(full.get('payload', {}))
            out.append({
                "message_id": full['id'],
                "from": headers.get('From', ''),
                "sender_email": extract_email(headers.get('From', '')),
                "subject": headers.get('Subject', '(no subject)'),
                "date": headers.get('Date', ''),
                "attachments": names,
                "attachment_bytes": total,
            })
        return out

    @staticmethod
    def describe(message: Dict) -> Dict:
        """Pull sender/subject/date out of a full message so the run is attributable."""
        payload = message.get('payload', {})
        headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}
        sender_raw = headers.get('from', '')
        return {
            "sender_email": extract_email(sender_raw),
            "sender_name": sender_raw.split('<')[0].strip().strip('"') or extract_email(sender_raw),
            "subject": headers.get('subject', '(no subject)'),
            "date": headers.get('date', ''),
            "snippet": message.get('snippet', '').strip(),
        }

    def download_attachment(self, message_id: str, attachment_id: str, filename: str, save_dir: str) -> str:
        try:
            attachment = self.service.users().messages().attachments().get(
                userId='me', messageId=message_id, id=attachment_id).execute()
            data = base64.urlsafe_b64decode(attachment['data'])
            filepath = os.path.join(save_dir, filename)
            with open(filepath, 'wb') as f:
                f.write(data)
            return filepath
        except HttpError as e:
            print(f"Failed to download attachment: {e}")
            return ""

    def mark_as_read(self, message_id: str):
        try:
            self.service.users().messages().modify(
                userId='me', id=message_id, body={'removeLabelIds': ['UNREAD']}).execute()
        except HttpError as e:
            print(f"Failed to mark as read: {e}")


class SlackClient:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.last_response = None   # (status_code, body) for diagnostics

    def send_notification(self, text: str, blocks: List[Dict] = None) -> bool:
        if not self.webhook_url:
            logger.warning("slack_webhook_missing")
            return False
        payload = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            self.last_response = (resp.status_code, resp.text[:400])
            if resp.status_code == 200:
                return True
            logger.warning("slack_rejected", extra={"status": resp.status_code, "body": resp.text[:200]})
            return False
        except Exception as exc:
            self.last_response = (None, f"{type(exc).__name__}: {exc}")
            logger.warning("slack_request_failed", extra={"error": str(exc)})
            return False


class CRMClient:
    def __init__(self, crm_base_url: str):
        self.base_url = crm_base_url
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=0.25, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET", "POST", "PUT"}))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def upsert_customer(self, email: str, name: str, company: str, notes: str) -> Dict:
        resp = self.session.put(f"{self.base_url}/api/v1/crm/customers", json={
            "email": email, "name": name, "company": company, "notes": notes
        }, timeout=10)
        return resp.json() if resp.status_code == 200 else {}

    def add_note(self, email: str, note: str) -> bool:
        resp = self.session.post(f"{self.base_url}/api/v1/crm/customers/{email}/notes", json={"note": note}, timeout=10)
        return resp.status_code == 200


def _write_demo_pdf(path: str, lines: List[str]) -> int:
    """Write a small but genuinely valid PDF so a downloaded proof artifact opens
    in any viewer instead of being a zero-byte placeholder."""
    content = ["BT", "/F1 16 Tf", "72 720 Td", f"(WorkFlowOS demo artifact) Tj", "0 -26 Td",
               f"/F1 11 Tf ({'generated by the automation executor'}) Tj"]
    for line in lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content += ["0 -18 Td", f"({escaped}) Tj"]
    content.append("ET")
    stream = "\n".join(content).encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()

    Path(path).write_bytes(bytes(out))
    return len(out)


class WorkflowExecutor:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.gmail = GmailClient(config.get('gmail_credentials', 'credentials.json'), config.get('gmail_token', 'token.json'))
        self.slack = SlackClient(config.get('slack_webhook', ''))
        self.crm = CRMClient(config.get('crm_url', 'http://localhost:8080'))
        self.download_dir = config.get('download_dir', './downloads')
        self.demo_mode = str(config.get('demo_mode', os.getenv('DEMO_MODE', 'false'))).lower() == 'true'
        self.dry_run = str(config.get('dry_run', os.getenv('DRY_RUN', 'false'))).lower() == 'true'
        self.browser_pool = BrowserPool()
        atexit.register(self.browser_pool.close)
        os.makedirs(self.download_dir, exist_ok=True)

    def execute(self, spec: WorkflowSpec) -> ExecutionResult:
        outputs: Dict[str, Any] = {}
        steps: List[Dict[str, Any]] = []
        # Runtime facts discovered during the run (sender, subject, filename...).
        # Later steps resolve {{tokens}} against this, which is what turns a
        # recorded demo into a workflow that works on any incoming email.
        context: Dict[str, Any] = dict(spec.variables or {})
        if not spec.actions:
            return ExecutionResult(False, "Workflow contains no actions", outputs, steps)

        for index, action in enumerate(spec.actions):
            step_no = action.get('step') or index + 1
            action_type = action.get('action', '')
            target = action.get('target', '')
            params = resolve(action.get('params', {}) or {}, context)
            started = time.perf_counter()

            try:
                if action_type == 'download_attachment':
                    result = self._download_attachment(params, context)
                elif action_type == 'lookup_customer':
                    result = self._lookup_customer(params)
                elif action_type == 'update_customer':
                    result = self._update_customer(params, outputs)
                elif action_type == 'send_slack_notification':
                    result = self._send_slack(params, context)
                elif action_type == 'open_url':
                    result = self._open_url(params)
                else:
                    raise ValueError(f"Unsupported action: {action_type}")

                missing = unresolved_tokens(params)
                if missing and not result.get('error'):
                    result = dict(result)
                    result['error'] = f"unresolved variable(s): {', '.join(sorted(set(missing)))}"
                    result['summary'] = f"cannot resolve {', '.join(sorted(set(missing)))}"

                status = 'failed' if result.get('error') else 'ok'
                steps.append(self._trace(step_no, action_type, target, status,
                                          result.get('summary', ''), result, started))
                outputs[f'step_{step_no}'] = result
                if status == 'failed':
                    return ExecutionResult(False, result['error'], outputs, steps)
            except Exception as exc:
                logger.exception("step_failed step=%s action=%s", step_no, action_type)
                steps.append(self._trace(step_no, action_type, target, 'failed',
                                          f"{type(exc).__name__}: {exc}", {"error": str(exc)}, started))
                return ExecutionResult(False, str(exc), outputs, steps)

        mode = "dry run" if self.dry_run else "execution"
        logger.info("workflow_%s_completed actions=%d", mode.replace(" ", "_"), len(spec.actions))
        return ExecutionResult(True, f"Workflow {mode} completed successfully", outputs, steps, context)

    @staticmethod
    def _trace(step: int, action: str, target: str, status: str,
               summary: str, evidence: Dict[str, Any], started: float) -> Dict[str, Any]:
        """One durable evidence record: what ran, how it ended, and what it produced."""
        return {
            "step": step,
            "action": action,
            "target": target,
            "status": status,
            "summary": summary,
            "evidence": {k: v for k, v in evidence.items() if k != "summary"},
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    def _download_attachment(self, params: Dict, context: Dict[str, Any]) -> Dict:
        if self.demo_mode:
            filename = params.get('filename', 'attachment.pdf')
            if not filename.lower().endswith('.pdf'):
                filename += '.pdf'
            filepath = os.path.join(self.download_dir, filename)
            sender = params.get('email_from') or 'customer@demo.com'
            subject = 'Demo customer request'
            size = _write_demo_pdf(filepath, [
                f"Source mailbox : {sender}",
                f"Subject        : {subject}",
                f"Generated at   : {datetime.now().isoformat(timespec='seconds')}",
                "",
                "This file is a real, downloadable artifact produced by the",
                "approved automation run. It is served from the backend at",
                "/api/v1/artifacts/<filename>.",
            ])
            context.update({"sender_email": sender, "subject": subject, "attachment_name": filename})
            return {
                "filepath": filepath, "filename": filename, "message_id": "demo-message",
                "bytes": size, "simulated": True, "sender_email": sender, "subject": subject,
                "summary": f"[DEMO] processed '{subject}' from {sender} -> {filename} ({size} bytes)",
            }

        self.gmail.authenticate()
        # Address-agnostic by default: take the newest unread email that has an
        # attachment. A sender filter is only applied when the spec pins one.
        email_from = params.get('email_from', '').strip()
        query = f"is:unread from:{email_from} has:attachment" if email_from else "is:unread has:attachment"
        msg = self.gmail.get_latest_unread(query)
        if not msg:
            return {"error": "No unread email with an attachment matched the trigger",
                    "summary": f"Gmail query returned nothing: {query}"}

        meta = self.gmail.describe(msg)
        sender = meta["sender_email"]
        subject = meta["subject"]
        context.update({
            "sender_email": sender,
            "sender_name": meta["sender_name"],
            "subject": subject,
            "message_id": msg["id"],
            "email_date": meta["date"],
        })

        saved = self._save_attachments(msg, subject)
        if not saved:
            return {"error": "No downloadable attachment in the matched email",
                    "subject": subject, "sender_email": sender,
                    "summary": f"matched '{subject}' from {sender} but it had no attachment part"}

        self.gmail.mark_as_read(msg["id"])
        first_path, first_name = saved[0]
        total = sum(os.path.getsize(p) for p, _ in saved if os.path.exists(p))
        context["attachment_name"] = first_name
        return {
            "filepath": first_path, "filename": first_name, "message_id": msg["id"],
            "bytes": total, "simulated": False,
            "sender_email": sender, "subject": subject, "email_date": meta["date"],
            "all_attachments": [n for _, n in saved],
            "summary": (f"Gmail: '{subject}' from {sender} -> saved {first_name} "
                        f"({total} bytes, {len(saved)} attachment(s))"),
        }

    def _save_attachments(self, msg: Dict, subject: str) -> List[tuple]:
        """Save every attachment part, walking nested multipart structures."""
        saved: List[tuple] = []
        safe_subject = re.sub(r'[^A-Za-z0-9._-]+', '_', subject)[:48] or 'email'
        stamp = datetime.now().strftime('%H%M%S')

        def walk(part: Dict):
            if part.get('parts'):
                for child in part['parts']:
                    walk(child)
                return
            filename = part.get('filename')
            attachment_id = part.get('body', {}).get('attachmentId')
            if not filename or not attachment_id:
                return
            base, ext = os.path.splitext(filename)
            target_name = f"{stamp}_{safe_subject}_{base}{ext}"[-120:]
            path = self.gmail.download_attachment(msg['id'], attachment_id, target_name, self.download_dir)
            if path:
                saved.append((path, target_name))

        walk(msg.get('payload', {}))
        return saved

    def _lookup_customer(self, params: Dict) -> Dict:
        email = params.get('email', '')
        if not email:
            return {"error": "No customer email resolved for this run", "summary": "no customer email available"}
        if self.dry_run:
            return {"customer": {"email": email}, "dry_run": True,
                    "summary": f"dry-run CRM lookup for {email}"}
        customer = self.crm.upsert_customer(email, "", "", "")
        name = (customer or {}).get("name") or "un-named"
        return {"customer": customer, "email": email,
                "summary": f"CRM {'found' if customer else 'created'} record for {email} (name: {name})"}

    def _update_customer(self, params: Dict, outputs: Dict) -> Dict:
        email = params.get('email', '')
        note = params.get('note', '')
        subject = params.get('subject', '')
        attachment_path = ""
        for value in outputs.values():
            if isinstance(value, dict) and value.get('filepath'):
                attachment_path = value['filepath']
                break
        lines = [note] if note else []
        if subject:
            lines.append(f"Email subject: {subject}")
        if attachment_path:
            lines.append(f"Attachment: {attachment_path}")
        note_text = "\n".join(lines) or "Customer request processed"

        if not email:
            return {"error": "No customer email resolved for this run", "summary": "cannot write a CRM note without an email"}
        if self.dry_run:
            return {"success": True, "note": note_text, "dry_run": True, "email": email,
                    "summary": f"dry-run CRM note for {email}"}
        success = self.crm.add_note(email, note_text)
        return {
            "success": success, "note": note_text, "email": email,
            "summary": (f"wrote {len(note_text.splitlines())}-line note to CRM record {email}"
                        if success else f"CRM note write failed for {email}"),
            "error": None if success else f"CRM note write failed for {email}",
        }

    def _send_slack(self, params: Dict, context: Dict[str, Any]) -> Dict:
        text = params.get('text', 'Workflow completed')
        for value in list(context.values()):
            text = text.replace(f"{{{{{value}}}}}", "")
        blocks = params.get('blocks')
        if self.demo_mode:
            return {"success": True, "text": text, "simulated": True, "dry_run": self.dry_run,
                    "summary": f"[DEMO] Slack message: {text}"}
        if self.dry_run:
            return {"success": True, "text": text, "dry_run": True,
                    "summary": f"dry-run Slack post: {text}"}
        success = self.slack.send_notification(text, blocks)
        return {
            "success": success, "text": text,
            "summary": (f"posted to the real Slack webhook: \"{text}\"" if success
                        else "Slack webhook rejected the message"),
            "error": None if success else "Slack webhook rejected the message",
        }

    def readiness(self) -> Dict[str, Any]:
        if self.demo_mode or self.dry_run:
            return {"ready": True, "mode": "demo" if self.demo_mode else "dry-run"}
        missing = []
        if not os.path.exists(self.gmail.credentials_file):
            missing.append("GMAIL_CREDENTIALS")
        if not self.slack.webhook_url:
            missing.append("SLACK_WEBHOOK_URL")
        return {"ready": not missing, "missing": missing, "mode": "live"}

    def _open_url(self, params: Dict) -> Dict:
        url = params.get('url', '')
        if not url:
            return {"error": "No URL provided", "summary": "no URL supplied"}
        page = self.browser_pool.page()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        page.close()
        return {"success": True, "url": url, "summary": f"opened {url} in Playwright Chromium"}


def main():
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    config = {
        'gmail_credentials': os.getenv('GMAIL_CREDENTIALS', 'credentials.json'),
        'gmail_token': os.getenv('GMAIL_TOKEN', 'token.json'),
        'slack_webhook': os.getenv('SLACK_WEBHOOK_URL', ''),
        'crm_url': os.getenv('CRM_URL', 'http://localhost:8080'),
        'download_dir': os.getenv('DOWNLOAD_DIR', './downloads'),
    }

    executor = WorkflowExecutor(config)

    if len(sys.argv) < 2:
        print("Usage: python executor.py <workflow_json_file>")
        sys.exit(1)

    with open(sys.argv[1], 'r') as f:
        workflow_data = json.load(f)

    spec = WorkflowSpec.from_dict(workflow_data.get('spec', workflow_data))
    result = executor.execute(spec)

    print(json.dumps({
        "success": result.success,
        "message": result.message,
        "outputs": result.outputs
    }, indent=2))


if __name__ == "__main__":
    main()
