# WorkFlowOS

AI-powered desktop agent that observes repetitive digital workflows, recognizes intent, generates structured automations, and executes them — all using free-tier APIs.

## Demo Scope
**Gmail (new customer email) → Download attachment → Look up customer in mock CRM → Update record with request + attachment → Notify Slack channel**

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Python Agent   │────▶│   Go Backend    │────▶│ Python Executor │
│  (capture)      │     │  (API + DB +    │     │  (Playwright +  │
│                 │     │   LLM + CRM)    │     │   Gmail/Slack)  │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                ▲
                                │
                        ┌───────┴───────┐
                        │  Web UI       │
                        │  (approval)   │
                        └───────────────┘
```

## Tech Stack
- **Go**: Backend API, SQLite (sqlx), repetition detection, mock CRM
- **Python**: Desktop capture (pynput, watchdog), execution (Playwright, Gmail API, Slack)
- **LLM**: OpenAI-compatible endpoint (Groq default, OpenRouter fallback)
- **UI**: Single HTML+JS file, no build step

## Quick Start

```bash
# 1. Start services (two terminals)
cd go && go run cmd/server/main.go                    # Terminal 1: Backend :8080
cd python && python -m executor.server                # Terminal 2: Executor :8082

# 2. Open http://localhost:8080 in a browser
```

For the no-credentials hackathon walkthrough, start the executor with `DEMO_MODE=true` and run `python test_e2e.py` from the project root. This uses a deterministic local workflow generator and simulated Gmail/Slack while still updating the real mock CRM. Add an LLM API key and real Gmail/Slack settings to use the live integrations.

## Free API Setup

| Service | Free Tier | Setup |
|---------|-----------|-------|
| **Groq** | 14.4k req/day, no card | [console.groq.com/keys](https://console.groq.com/keys) |
| **OpenRouter** | Free models, no card | [openrouter.ai/keys](https://openrouter.ai/keys) |
| **Gmail API** | Free | Google Cloud Console → Enable Gmail API → OAuth Desktop |
| **Slack** | Free tier | [api.slack.com/apps](https://api.slack.com/apps) → Incoming Webhook |

See [SETUP.md](SETUP.md) for detailed instructions.

## Project Structure

```
WorkFlowOS/
├── go/
│   ├── cmd/server/main.go          # Entry point
│   ├── internal/
│   │   ├── config/                 # YAML config loader
│   │   ├── db/                     # SQLite schema + init
│   │   ├── model/                  # Data models
│   │   ├── service/                # Detection, workflow, CRM
│   │   ├── handler/                # REST endpoints
│   │   └── llm/                    # OpenAI-compatible client
│   ├── config.yaml                 # Configuration
│   └── go.mod
├── python/
│   ├── agent/capture.py            # Desktop activity capture
│   ├── executor/
│   │   ├── executor.py             # Workflow execution logic
│   │   └── server.py               # Flask REST server
│   ├── shared/events.py            # Shared data types
│   └── requirements.txt
├── web/index.html                  # Approval UI
├── SETUP.md                        # Detailed setup guide
├── DEMO.md                         # Live demo script
├── docker-compose.yml              # Container orchestration
└── test_e2e.py                     # End-to-end test
```

## Key Design Decisions

1. **Repetition detection ≠ AI** — Simple n-gram frequency matching (4-6 step windows, 3+ occurrences). O(1) per event, no LLM cost.

2. **Single LLM call** — Only for "understanding": sequence → intent + structured workflow JSON. Uses `response_format: json_object`.

3. **Provider-agnostic LLM** — One config line switches between Groq/OpenRouter. No vendor SDKs.

4. **Mock CRM** — SQLite + REST. Replace with real CRM by implementing same endpoints.

5. **Local-first** — All data stays local. Only action sequences (no content) sent to LLM.

## Workflow JSON Schema (LLM Output)

```json
{
  "intent": "string",
  "trigger": {"type": "string", "source": "string"},
  "actions": [
    {"step": 1, "action": "download_attachment", "target": "gmail", "params": {}}
  ],
  "conditions": [{"if": "string", "then": "string"}],
  "variables": {"customer_email": "customer@demo.com"}
}
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/events/ingest` | Ingest observed event |
| GET | `/api/v1/sequences` | List detected sequences |
| POST | `/api/v1/workflows/generate` | Generate workflow from sequence |
| GET | `/api/v1/workflows` | List workflows |
| GET | `/api/v1/workflows/:id` | Get workflow detail |
| POST | `/api/v1/workflows/approve` | Approve/reject workflow |
| POST | `/api/v1/workflows/execute` | Execute approved workflow |
| POST | `/api/v1/crm/customers` | Create customer |
| GET | `/api/v1/crm/customers/:email` | Get customer |
| PUT | `/api/v1/crm/customers` | Upsert customer |
| POST | `/api/v1/crm/customers/:email/notes` | Add note |

## License

MIT — Hackathon MVP, use freely.
