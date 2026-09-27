# WorkFlowOS - Setup Instructions

## Prerequisites
- Go 1.21+
- Python 3.10+
- SQLite3 (usually pre-installed)

---

## 1. Groq API Key (Primary - Free, No Card Required)

1. Go to https://console.groq.com/keys
2. Sign up with GitHub/Google (no credit card required)
3. Create a new API key
4. Copy the key (format: `gsk_...`)

**Daily Limits (Free Tier):**
- 14,400 requests/day
- 30 RPM
- 6,000 TPM

---

## 2. OpenRouter API Key (Backup - Free Tier)

1. Go to https://openrouter.ai/keys
2. Sign up (no credit card required)
3. Create a new API key
4. Copy the key (format: `sk-or-v1-...`)

**Free Models Available:**
- `meta-llama/llama-3-8b-instruct:free`
- `mistralai/mistral-7b-instruct:free`
- `google/gemma-7b-it:free`

**To use OpenRouter instead of Groq:**
Edit `go/config.yaml`:
```yaml
llm:
  base_url: "https://openrouter.ai/api/v1"
  model: "meta-llama/llama-3-8b-instruct:free"
  api_key: ""  # Set via LLM_API_KEY env var
```

---

## 3. Gmail API Setup

1. Go to https://console.cloud.google.com/
2. Create a new project (or select existing)
3. Enable Gmail API: APIs & Services → Library → Gmail API → Enable
4. Create OAuth 2.0 credentials:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: Desktop app
   - Name: WorkFlowOS
5. Download JSON → save as `go/credentials.json` (or `python/credentials.json`)
6. Add test users in OAuth consent screen (your Gmail)

---

## 4. Slack Incoming Webhook

1. Go to https://api.slack.com/apps
2. Create New App → From scratch → Name: WorkFlowOS → Select workspace
3. Features → Incoming Webhooks → Activate
4. Add New Webhook to Workspace → Select channel → Allow
5. Copy Webhook URL (format: `https://hooks.slack.com/services/...`)

---

## 5. Environment Variables

Create `.env` in project root:
```bash
# Required
LLM_API_KEY=your_groq_or_openrouter_key
SLACK_WEBHOOK_URL=your_slack_webhook_url

# Optional
GMAIL_CREDENTIALS=./credentials.json
GMAIL_TOKEN=./token.json
CRM_URL=http://localhost:8080
PYTHON_EXECUTOR_URL=http://localhost:8082
PORT=8080
```

---

## 6. Install Dependencies

### Go Backend
```bash
cd go
go mod tidy
```

### Python Services
```bash
cd python
pip install -r requirements.txt
playwright install chromium
```

---

## 7. Run Services

### Terminal 1: Go Backend
```bash
cd go
go run cmd/server/main.go
# Runs on http://localhost:8080
```

### Terminal 2: Python Executor
```bash
cd python
python -m executor.server
# Runs on http://localhost:8082
```

### Terminal 3: Activity Capture Agent
```bash
cd python
python -m agent.capture
# Starts capturing desktop activity
```

### Terminal 4: (Optional) Mock CRM only - already included in Go backend

---

## 8. Verify Setup

1. Open http://localhost:8080 - should see WorkFlowOS UI
2. Check both badges show "✓" (green)
3. Send a test email with attachment to your Gmail
4. Perform the workflow manually 3+ times:
   - Open Gmail → Open email → Download attachment → (simulate CRM update) → (simulate Slack notify)
5. Check "Detected Sequences" panel - should show candidate sequence
6. Click sequence → "Generate Workflow" → Review → Approve → Execute

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| LLM returns invalid JSON | Check API key, try OpenRouter fallback |
| Gmail auth fails | Delete `token.json`, re-run to re-auth |
| Playwright errors | Run `playwright install chromium` |
| No sequences detected | Lower threshold in `service.go` (count >= 2) |
| CORS errors | Check `corsMiddleware()` in `main.go` |

---

## Switching LLM Providers

Edit single line in `go/config.yaml`:
```yaml
# Groq (default)
base_url: "https://api.groq.com/openai/v1"
model: "llama3-8b-8192"

# OpenRouter (backup)
base_url: "https://openrouter.ai/api/v1"
model: "meta-llama/llama-3-8b-instruct:free"
```
Restart Go backend after change.