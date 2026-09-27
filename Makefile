.DEFAULT_GOAL := help

.PHONY: help run-backend run-executor run-agent demo test lint health proofs

help:
	@echo "Targets: run-backend run-executor run-agent demo test lint health proofs"

# The artifact root comes from go/config.yaml (artifacts.dir), so the backend
# serves real automation artifacts from /api/v1/artifacts/* with no env var.
# Override with ARTIFACT_DIR only if you move the downloads folder.
run-backend:
	cd go && go run cmd/server/main.go

run-executor:
	cd python && DEMO_MODE=true python -m executor.server

run-agent:
	cd python && python -m agent.capture

demo:
	cd python && python ../test_e2e.py

test:
	cd go && go test ./...
	cd python && python -m unittest discover -s tests

lint:
	cd go && gofmt -d $$(find . -name '*.go')
	cd python && python -m py_compile agent/capture.py executor/executor.py executor/server.py

health:
	@curl -fsS http://localhost:8080/healthz
	@curl -fsS http://localhost:8080/readyz
	@curl -fsS http://localhost:8082/health
	@curl -fsS http://localhost:8082/readyz

# Prints the evidence trail of the most recent run: per-step outcomes and every
# verifiable artifact. Use this when you need proof without opening the browser.
proofs:
	@python -c "import json,urllib.request;ws=json.load(urllib.request.urlopen('http://localhost:8080/api/v1/workflows'))['workflows'];print('no runs yet' if not ws else json.dumps(json.load(urllib.request.urlopen('http://localhost:8080/api/v1/workflows/'+ws[0]['id']+'/proofs')),indent=2))"
