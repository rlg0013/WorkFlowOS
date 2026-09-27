# WorkFlowOS — Demo Guide

> **Pitch:** WorkFlowOS watches a repetitive cross-app task, learns the pattern, proposes an automation, and **shows you proof it actually ran** — before you approve anything.

The whole demo is one loop: **Observe → Discover → Review → Automate → Prove**.

---

## 1. Before you start (2 minutes)

Open **three PowerShell windows** in the repo root (`WorkFlowOS`).

### Window 1 — backend (serves the dashboard + API)

```powershell
cd go
go run cmd/server/main.go
```

### Window 2 — automation executor

```powershell
cd python
$env:DEMO_MODE='true'
python -m executor.server
```

### Window 3 — leave open, this is your proof terminal

Nothing to start here. You will use it in step 6.

**Sanity check** — all four must answer:

```powershell
curl.exe -s http://localhost:8080/healthz     # {"service":"workflowos","status":"ok"}
curl.exe -s http://localhost:8080/readyz      # {"status":"ready"}
curl.exe -s http://localhost:8082/health      # {"status":"ok"}
curl.exe -s http://localhost:8082/readyz      # {"mode":"demo","ready":true}
```

Then open **http://localhost:8080** in Chrome. You should see a dark terminal-style console with a green **LOCAL SERVICES ONLINE** badge.

> `DEMO_MODE=true` means Gmail and Slack are simulated so the demo never depends on
> Wi-Fi, OAuth, or a webhook. Everything else — detection, workflow lifecycle,
> execution, CRM writes, artifact generation — is the real code path.

---

## 2. Run the demo (60 seconds)

### Click `RUN_GUIDED_DEMO`

Watch the pipeline bar at the top advance: `OBSERVE → DISCOVER → REVIEW`.

What just happened, in order:

| Step | What the system did |
|---|---|
| Observe | Recorded the customer-request task **3 times** (email → attachment → CRM → team notification) |
| Discover | The frequency matcher found the same 4-step pattern 3+ times and flagged it as a candidate |
| Review | Generated a structured workflow draft — intent, trigger, 4 actions, conditions, variables |

You now have a **PENDING** workflow with four proposed actions listed. Nothing has run yet.

---

## 3. Approve (10 seconds)

Click **`APPROVE_WORKFLOW`**.

Status flips `PENDING → APPROVED` and the button becomes **`RUN_APPROVED_AUTOMATION`**.

> Say this: *"Discovery is not permission. A human approves the exact actions before anything touches an external system."*

---

## 4. Execute — and actually watch it (20 seconds)

Click **`RUN_APPROVED_AUTOMATION`**.

**Look at the `LIVE_RUN_TRACE` panel on the left.** The dashboard polls the backend
every 400 ms, so you see each action light up in real time:

```
●  step 1 · download_attachment → gmail      running…
✓  step 2 · lookup_customer → crm                4ms
✓  step 3 · update_customer → crm                1ms
✓  step 4 · send_slack_notification → slack      0ms
```

Each row shows the real summary the executor returned and the real duration. The
`EXECUTION_LOG` below prints one line per step with its outcome.

The pipeline bar advances to `AUTOMATE` and the workflow status becomes `COMPLETED`.

---

## 5. Proofs — the part judges actually care about (30 seconds)

Scroll to **`PROOF_ARTIFACTS`**. This is not a claim, it is evidence:

| Proof | What it shows |
|---|---|
| `artifact` | **`contract.pdf`** with a green **DOWNLOAD contract.pdf** button |
| `crm_record` | The exact note text written to the CRM |
| `notification` | The exact message posted to the team channel |

### Click `DOWNLOAD contract.pdf`

You get a real **1,027-byte valid PDF** that opens in any viewer, listing the source
mailbox, the generator, and the timestamp. That file was written by the automation
into `python/downloads/` and is being served to your browser by the Go backend.

> Say this: *"This is an auditable execution record, not a black-box macro. Click the
> artifact — it's the file the automation produced, not a screenshot."*

---

## 6. Prove it without the browser

If a judge does not want to look at a UI, use **Window 3**.

```powershell
python proofs.py
```

That prints the whole evidence trail — status, per-step timings, and every proof with
a download link:

```
steps     : 4/4 ok, 0 failed

PER-STEP TRACE
  [ok] step 1      0ms  simulated download of contract.pdf (1027 bytes)
  [ok] step 2      1ms  CRM record found for customer@demo.com
  ...

PROOF ARTIFACTS
  [VERIFIED] artifact      contract.pdf
             download: http://localhost:8080/api/v1/artifacts/contract.pdf
  [VERIFIED] crm_record    CRM note written
  [VERIFIED] notification  Team notification
```

Or hit the API directly:

```powershell
# every step of the last run, with timings
curl.exe -s http://localhost:8080/api/v1/workflows | python -c "import json,sys;print(json.load(sys.stdin)['workflows'][0]['id'])" > $env:TEMP\wid.txt
$wid = Get-Content $env:TEMP\wid.txt

curl.exe -s "http://localhost:8080/api/v1/workflows/$wid/steps"    # per-step trace
curl.exe -s "http://localhost:8080/api/v1/workflows/$wid/proofs"   # proof bundle
curl.exe -s http://localhost:8080/api/v1/crm/customers/customer%40demo.com
```

> If you have GNU `make` installed, `make proofs` and `make test` are equivalent shortcuts.

---

## 7. Automated verification

Run the whole loop plus every proof assertion without touching the UI:

```powershell
python test_e2e.py
```

Expected tail:

```
    step 1     ok    0ms  simulated download of contract.pdf (1027 bytes)
    step 2     ok    1ms  CRM record found for customer@demo.com
    step 3     ok    1ms  appended note to customer@demo.com
    step 4     ok    0ms  simulated Slack post: Processed customer request
[OK] 4 steps readable at /workflows/87d0ccaf/steps
    proof artifact      contract.pdf           verified=True
    proof crm_record    CRM note written       verified=True
    proof notification  Team notification      verified=True
[OK] Artifact downloadable (1027 bytes) at /api/v1/artifacts/contract.pdf
[OK] Artifact route refuses path traversal
[OK] CRM record contains the note and attachment reference
ALL TESTS PASSED - automation ran and its proofs are reachable
```

Unit tests:

```powershell
cd go     ; go test ./...
cd python ; python -m unittest discover -s tests
```

---

## 8. Why the run is trustworthy

| Question | Answer |
|---|---|
| Are the steps real? | Yes. They are written by the Python executor, persisted to the `execution_steps` table, and served by `GET /workflows/:id/steps`. |
| Could the UI be faking success? | No. `LIVE_RUN_TRACE` renders only what the backend returns. If a step fails, the row turns red and the workflow is marked `failed`. |
| Where do proofs come from? | `GET /workflows/:id/proofs` derives them from the stored step evidence — artifact path, CRM note, notification text. |
| Is the PDF real? | Yes, a valid PDF with a correct xref table. Download it and open it. |
| Can someone read arbitrary files? | No. The artifact route rejects path traversal with HTTP 400 (covered by a test). |
| What if the executor is down? | Steps are pre-marked `running`; if the call fails they are marked `failed` with the reason instead of hanging. |

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `LOCAL SERVER OFFLINE` (red badge) | Window 1 is not running. `cd go; go run cmd/server/main.go` |
| Button says *executor unavailable* | Window 2 is not running, or `DEMO_MODE` is not set. |
| `LIVE_RUN_TRACE` says `No run yet` | You have not clicked `RUN_APPROVED_AUTOMATION` yet. |
| `DOWNLOAD` button 404s | `artifacts.dir` in `go/config.yaml` must point at `python/downloads`. |
| Steps stuck on `running` | Executor crashed mid-run. Restart Window 2 and re-run. |
| Port already in use | `Get-NetTCPConnection -LocalPort 8080,8082 -State Listen \| Stop-Process` |
| Want a clean slate | Delete `go/workflowos.db` and restart Window 1. |
| Want to show real Gmail/Slack | Set `LLM_API_KEY`, `SLACK_WEBHOOK_URL`, `GMAIL_CREDENTIALS` and drop `DEMO_MODE`. See SETUP.md. |

---

## 10. Running against REAL email and a REAL Slack channel

The guided demo above is simulated on purpose so it never fails on stage. To run
the identical pipeline against your own inbox, do the following.

### What changes

| | Demo mode | Live mode |
|---|---|---|
| `DEMO_MODE` | `true` | unset / `false` |
| Gmail | fabricated PDF | **real** message via Gmail API, real attachments downloaded |
| Sender | `customer@demo.com` | **resolved at run time** from the `From` header |
| Subject | "Demo customer request" | **the real subject line** |
| Slack | logged locally | **a real message in your channel** |
| LLM | local template | optional real Groq/OpenRouter call |

### Step 1 — one-time setup (creates your .env)

```powershell
python setup.py
```

It asks for two values, writes a BOM-free `.env` (gitignored), then runs the
preflight automatically:

```
  SLACK_WEBHOOK_URL: https://hooks.slack.com/services/T.../B.../xxx
  LLM_API_KEY: gsk_...                       (press Enter to skip)
  Provider? [g]roq / [o]penrouter: g

  Wrote .env (no BOM, 438 bytes)
```

`python/credentials.json` already exists. Authorize once:

```powershell
python doctor.py --auth
```

It prints a labelled Google consent URL, tries to open a browser, and waits:

```
  STEP 1 of 2 - approve access in your browser
    listening on http://localhost:63906

  If no browser opened, paste this URL into any browser:

    https://accounts.google.com/o/oauth2/v2/auth?client_id=...&scope=...

  Sign in, then on the warning screen click
  Advanced -> "Go to WorkFlowOS (unsafe)" to continue.

  STEP 2 of 2 - waiting for you to approve
  [ok] Authorized: you@gmail.com
  [ok] Token saved to python\token.json
```

**Two different Google messages — they are not the same problem:**

| What you see | What it means | Fix |
|---|---|---|
| *"Google hasn't verified this app"* **with** an `Advanced` link | Normal for a personal project | Click `Advanced` → `Go to WorkFlowOS (unsafe)` |
| *"Access blocked: ... has not completed the Google verification process"* **with no** `Advanced` link | Your account is **not a test user**. No bypass exists | Add yourself below, then retry |

To fix the hard block:

1. Open **https://console.cloud.google.com** and select project **`workflowos-509805`**
2. **APIs & Services → Google Auth Platform** (older UI: *OAuth consent screen*)
3. Make sure **Publishing status = Testing**
4. Under **Test users** → **ADD USERS** → enter **your own Gmail address** → **ADD**
5. Click **SAVE** (not just Add)
6. Wait 1–2 minutes, then re-run `python doctor.py --auth`

Do **not** click *Request verification* — unnecessary for your own account, and it
takes days. If you cannot see the *Test users* section, the project is owned by a
Google Workspace organisation whose admin restricts OAuth; use a personal Gmail as
the test user instead.

Prefer to edit by hand? `Copy-Item .env.example .env` and edit `.env` directly.

### Step 2 — check everything and see your real inbox

```powershell
python doctor.py --inbox
```

This prints your **actual** unread emails with attachments:

```
GMAIL
  [ok] OAuth client credentials present - python/credentials.json
  [ok] Authorized token present - python/token.json

REAL CANDIDATE EMAILS  (is:unread has:attachment)
  from    : "Jane Doe" <jane.doe@acme-corp.com>
  sender  : jane.doe@acme-corp.com
  subject : Contract review - Acme Q3
  date    : Mon, 22 Sep 2025 10:14:02 +0530
  files   : contract.pdf, pricing.xlsx
  id      : 18f2a1b2c3d4e5f6
```

If it says *no candidates*, send yourself an email with a file attached and **leave it
unread** — the trigger is `is:unread has:attachment`.

### Step 3 — connect Slack and verify it

Already done by `setup.py`. Confirm it landed:

```powershell
python doctor.py --slack
```

`[ok] Test message delivered` means a real message is in your channel.

### Step 4 — run the whole thing for real

Your `.env` already sets `DEMO_MODE=false`, so:

```powershell
# Window 1: backend (unchanged)
cd go; go run cmd/server/main.go

# Window 2: executor in LIVE mode
cd python
python -m executor.server
```

```powershell
# Window 3: the real run
python live_run.py --list        # see candidates, change nothing
python live_run.py --run --slack # full live pipeline
```

Output looks like this — every line is a real integration:

```
=== 3. TARGET SELECTED ===
  [ok] will process: 'Contract review - Acme Q3' from jane.doe@acme-corp.com

=== 7. EXECUTE FOR REAL ===
  [ok] step 1   412ms  Gmail: 'Contract review - Acme Q3' from jane.doe@acme-corp.com
                      -> saved 143052_Contract_review_-__Acme_Q3_contract.pdf
                      (48213 bytes, 2 attachment(s))
  [ok] step 2     8ms  CRM found record for jane.doe@acme-corp.com
  [ok] step 3     3ms  wrote 3-line note to CRM record jane.doe@acme-corp.com
  [ok] step 4   311ms  posted to the real Slack webhook: "New customer email
                      processed: Contract review - Acme Q3 (from jane.doe@acme-corp.com...)"
```

Then open Slack — the message is really there. And `python proofs.py` lists the
downloaded file with a direct link.

### How the automation stays general

The generated workflow never hardcodes an address. It contains runtime placeholders
that the executor fills from the email that actually arrived:

```json
{"step": 1, "action": "download_attachment", "target": "gmail",
 "params": {"query": "is:unread has:attachment"}}
{"step": 2, "action": "lookup_customer", "target": "crm",
 "params": {"email": "{{sender_email}}"}}
{"step": 3, "action": "send_slack_notification", "target": "slack",
 "params": {"text": "New email from {{sender_email}}: {{subject}}"}}
```

Available placeholders: `{{sender_email}}`, `{{sender_name}}`, `{{subject}}`,
`{{attachment_name}}`, `{{message_id}}`, `{{email_date}}`.

If a step references a placeholder the run never produced, it **fails visibly**
rather than silently sending a literal `{{sender_email}}` to Slack. That behaviour is
covered by `test_unresolved_token_fails_the_step_visibly`.

### Add a real LLM (optional)

```powershell
$env:LLM_API_KEY='gsk_...'   # console.groq.com/keys - free, no card
```

Restart the backend. Workflow generation now calls the model instead of the local
template. The system prompt explicitly forbids hardcoding observed addresses and
requires the placeholders above, so a real model produces the same general spec.

---

## 11. FAQ

**Is the guided demo fake?**
The *observations* are seeded. Everything after that is production code: repetition
detection, workflow generation, the approval gate, the executor HTTP call, per-step
persistence, CRM mutation, and artifact generation. Only Gmail and Slack are simulated.

**Where is the desktop agent?**
`python/agent/capture.py`. It is optional for the demo and not started by default,
because raw clicks are not a reliable or privacy-safe signal. The demo uses semantic
events; production adapters map Gmail/CRM/Slack actions into the same event shape.

**Why can't it just run immediately?**
A discovered pattern is a recommendation, not consent. The approval gate is the point.

**What leaves the machine?**
In `DEMO_MODE`, nothing. With an LLM key set, only the privacy-filtered event
structure is sent to the configured OpenAI-compatible endpoint.

**Can I skip the UI entirely?**
Yes — `python test_e2e.py` runs the full loop and asserts every proof.
