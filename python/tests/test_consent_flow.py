"""Verify the consent flow without touching Google.

A fake authorization server stands in for accounts.google.com: we check that the
flow builds a correct consent URL, that our loopback listener captures the ?code=
redirect, that the code is exchanged, and that the resulting token is persisted.
"""
import json
import os
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

# The stand-in auth server is plain HTTP on loopback. The real flow talks to
# https://accounts.google.com, so this flag is safe and test-only.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from executor.executor import GmailAuthError, GmailClient  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402

captured_exchange = {}
captured_consent = {}
fail_token_endpoint = {"on": False}


class FakeGoogle(BaseHTTPRequestHandler):
    """Mimics the two Google endpoints the installed-app flow touches."""

    def do_GET(self):  # noqa: N802
        # The consent screen. A real browser shows "app not verified" here and
        # the user clicks through; we just redirect back with a code.
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        captured_consent.update(params)
        redirect = params.get("redirect_uri", "")
        state = params.get("state", "")
        if redirect:
            target = f"{redirect}?code=fake-auth-code&state={state}"
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
        else:
            self.send_response(400)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = urllib.parse.parse_qsl(self.rfile.read(length).decode())
        fields = dict(body)
        captured_exchange.update(fields)
        if fail_token_endpoint["on"]:
            raw = b'{"error":"invalid_grant","error_description":"Bad Request"}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        payload = {
            "access_token": "ya29.fake-access",
            "refresh_token": "1//fake-refresh",
            "token_type": "Bearer",
            "expires_in": 3599,
        }
        # Echo scope only if the request carried one; returning an empty scope
        # makes oauthlib treat it as a scope change and reject the response.
        if fields.get("scope"):
            payload["scope"] = fields["scope"]
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def run_flow(creds_path, token_path, google_base, approve=True, favicon_after=False):
    """Drive the real consent flow, optionally auto-approving the consent page."""
    import webbrowser
    real_open = webbrowser.open

    def fake_open(url):
        if not approve:
            return True

        def visit():
            try:
                urllib.request.urlopen(url, timeout=20).read()
            except Exception as exc:
                print("  [test] simulated browser visit failed:", type(exc).__name__, exc)
            if favicon_after:
                # Exactly what Chrome/Firefox do next: ask for /favicon.ico on
                # the same origin. This must not clobber a successful callback.
                try:
                    urllib.request.urlopen(url.rsplit("?", 1)[0] + "favicon.ico",
                                            timeout=10).read()
                except Exception:
                    pass
        timer = threading.Timer(0.3, visit)
        timer.daemon = True          # never let the harness hang on shutdown
        timer.start()
        return True

    webbrowser.open = fake_open
    try:
        gmail = GmailClient(creds_path, token_path, auth_timeout=25)
        return gmail, gmail.authenticate(interactive=True)
    finally:
        webbrowser.open = real_open


def main():
    server = HTTPServer(("127.0.0.1", 0), FakeGoogle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    google_base = f"http://127.0.0.1:{server.server_address[1]}"

    workdir = tempfile.mkdtemp()
    creds_path = os.path.join(workdir, "credentials.json")
    json.dump({
        "installed": {
            "client_id": "test.apps.googleusercontent.com",
            "client_secret": "test-secret",
            "auth_uri": f"{google_base}/o/oauth2/auth",
            "token_uri": f"{google_base}/o/oauth2/token",
            "redirect_uris": ["http://localhost"],
        }
    }, open(creds_path, "w"))

    token_path = os.path.join(workdir, "token.json")

    gmail, _ = run_flow(creds_path, token_path, google_base)
    server.shutdown()

    print("\n--- happy path ---")
    ok = True

    def expect(label, condition, detail=""):
        nonlocal ok
        print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
        ok = ok and condition

    expect("consent screen was reached", bool(captured_consent.get("client_id")),
           f"client_id={captured_consent.get('client_id')}")
    expect("requested an offline refresh token",
           captured_consent.get("access_type") == "offline",
           f"access_type={captured_consent.get('access_type')}")
    expect("forced the consent prompt", captured_consent.get("prompt") == "consent")
    expect("PKCE challenge present (S256)",
           captured_consent.get("code_challenge_method") == "S256")
    expect("redirect_uri was a loopback address",
           captured_consent.get("redirect_uri", "").startswith("http://localhost:"),
           captured_consent.get("redirect_uri", ""))
    expect("authorization code received and exchanged",
           captured_exchange.get("code") == "fake-auth-code",
           f"code={captured_exchange.get('code')}")
    expect("redirect_uri sent on the token exchange",
           captured_exchange.get("redirect_uri", "").startswith("http://localhost:"))
    expect("token persisted to disk", os.path.exists(token_path))

    saved = json.load(open(token_path)) if os.path.exists(token_path) else {}
    expect("saved access token", bool(saved.get("token")))
    expect("saved refresh token", bool(saved.get("refresh_token")))
    expect("saved token_uri", bool(saved.get("token_uri")))
    expect("saved client_id", bool(saved.get("client_id")))

    # The saved file must be reloadable by the normal auth path.
    reloaded = Credentials.from_authorized_user_file(token_path, [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify"])
    expect("saved token reloads via Credentials", bool(reloaded.refresh_token))
    expect("scopes persisted", len(reloaded.scopes or []) == 2, str(reloaded.scopes))

    # Regression guard: the browser page must not claim success when the token
    # exchange fails. This is exactly the silent-failure the user hit.
    print("\n--- token exchange failure path ---")
    server2 = HTTPServer(("127.0.0.1", 0), FakeGoogle)
    threading.Thread(target=server2.serve_forever, daemon=True).start()
    base2 = f"http://127.0.0.1:{server2.server_address[1]}"
    bad_creds = os.path.join(workdir, "bad_credentials.json")
    cfg = json.load(open(creds_path))
    cfg["installed"]["auth_uri"] = f"{base2}/o/oauth2/auth"
    cfg["installed"]["token_uri"] = f"{base2}/o/oauth2/token"
    json.dump(cfg, open(bad_creds, "w"))

    fail_token_endpoint["on"] = True
    bad_token = os.path.join(workdir, "bad_token.json")
    if os.path.exists(bad_token):
        os.remove(bad_token)
    try:
        run_flow(bad_creds, bad_token, base2)
        expect("failed exchange raises instead of reporting success", False,
               "authenticate() returned normally")
    except GmailAuthError as exc:
        expect("failed exchange raises instead of reporting success", True, exc.reason)
        expect("classified as exchange_failed", exc.reason == "exchange_failed", exc.reason)
    except Exception as exc:
        expect("failed exchange raises instead of reporting success", False,
               f"{type(exc).__name__}: {exc}")
    finally:
        fail_token_endpoint["on"] = False
        server2.shutdown()

    expect("no token written when the exchange fails", not os.path.exists(bad_token))

    # Regression: Chrome/Firefox request /favicon.ico right after the OAuth
    # callback. That request must not be mistaken for a failed authorization.
    print("\n--- stray favicon request after a successful callback ---")
    server3 = HTTPServer(("127.0.0.1", 0), FakeGoogle)
    threading.Thread(target=server3.serve_forever, daemon=True).start()
    base3 = f"http://127.0.0.1:{server3.server_address[1]}"
    ok_creds = os.path.join(workdir, "ok_credentials.json")
    cfg2 = json.load(open(creds_path))
    cfg2["installed"]["auth_uri"] = f"{base3}/o/oauth2/auth"
    cfg2["installed"]["token_uri"] = f"{base3}/o/oauth2/token"
    json.dump(cfg2, open(ok_creds, "w"))

    ok_token = os.path.join(workdir, "ok_token.json")
    if os.path.exists(ok_token):
        os.remove(ok_token)
    try:
        run_flow(ok_creds, ok_token, base3, favicon_after=True)
        expect("favicon request does not break authorization", True)
        expect("token still written despite favicon", os.path.exists(ok_token))
    except GmailAuthError as exc:
        expect("favicon request does not break authorization", False, f"{exc.reason}: {exc}")
    except Exception as exc:
        expect("favicon request does not break authorization", False,
               f"{type(exc).__name__}: {exc}")
    finally:
        server3.shutdown()

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
