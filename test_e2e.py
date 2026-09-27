#!/usr/bin/env python3
"""Quick end-to-end test of the WorkFlowOS pipeline."""

import json
import time
import requests
import sys
import uuid

BASE_URL = "http://localhost:8080/api/v1"
EXECUTOR_URL = "http://localhost:8082"

def test_health():
    print("Testing health endpoints...")
    r = requests.get(f"{BASE_URL}/workflows")
    assert r.status_code == 200, "Backend not responding"
    r = requests.get(f"{EXECUTOR_URL}/health")
    assert r.status_code == 200, "Executor not responding"
    print("[OK] Health checks passed")

def test_ingest_events():
    print("Ingesting test events...")
    session_id = f"test_session_{uuid.uuid4()}"
    inbox_target = f"inbox_{session_id[-8:]}"
    events = [
        {"source": "gmail", "action": "open", "target": inbox_target, "payload": "", "session_id": session_id},
        {"source": "gmail", "action": "click", "target": "email_1", "payload": "from:customer@demo.com", "session_id": session_id},
        {"source": "gmail", "action": "download", "target": "attachment", "payload": "contract.pdf", "session_id": session_id},
        {"source": "crm", "action": "lookup", "target": "customer", "payload": "customer@demo.com", "session_id": session_id},
        {"source": "crm", "action": "update", "target": "record", "payload": "added note", "session_id": session_id},
        {"source": "slack", "action": "notify", "target": "channel", "payload": "new request", "session_id": session_id},
    ]
    for _ in range(3):
        for e in events:
            r = requests.post(f"{BASE_URL}/events/ingest", json=e)
            assert r.status_code == 200, f"Failed to ingest: {r.text}"
            time.sleep(0.05)
    print("[OK] Events ingested")
    return inbox_target

def test_sequence_detection(inbox_target):
    print("Waiting for sequence detection...")
    time.sleep(3)
    r = requests.get(f"{BASE_URL}/sequences")
    assert r.status_code == 200
    data = r.json()
    assert len(data.get("sequences", [])) > 0, "No sequences detected"
    seq = next((s for s in data["sequences"] if f"gmail:open:{inbox_target}" in s["pattern"]), None)
    assert seq, "Expected Gmail-to-Slack sequence was not detected"
    assert seq["count"] >= 3, f"Sequence count too low: {seq['count']}"
    print(f"[OK] Sequence detected: {seq['id']} (count: {seq['count']})")
    return seq["id"]

def test_workflow_generation(seq_id):
    print("Generating workflow via LLM...")
    r = requests.post(f"{BASE_URL}/workflows/generate", json={"sequence_id": seq_id})
    assert r.status_code == 200, f"Generation failed: {r.text}"
    data = r.json()
    wf = data["workflow"]
    assert wf["status"] == "pending"
    print(f"[OK] Workflow generated: {wf['id']} - {wf['intent']}")
    return wf["id"]

def test_approval(wf_id):
    print("Approving workflow...")
    r = requests.post(f"{BASE_URL}/workflows/approve", json={"workflow_id": wf_id, "approved": True})
    assert r.status_code == 200
    print("[OK] Workflow approved")
def test_execution(wf_id):
    print("Executing workflow...")
    r = requests.post(f"{BASE_URL}/workflows/execute", json={"workflow_id": wf_id})
    assert r.status_code == 200, f"Execution failed: {r.text}"
    data = r.json()
    assert data["status"] == "completed", f"Execution failed: {data}"

    steps = data.get("steps") or []
    assert steps, "executor returned no step trace"
    for step in steps:
        print(f"    step {step['step']} {step['status']:>6} {step['duration_ms']:>4}ms  {step['summary']}")
    assert all(s["status"] == "ok" for s in steps), "not every step completed"
    print(f"[OK] Workflow executed: {len(steps)} steps traced")
    return data


def test_steps_persisted(wf_id):
    print("Checking durable step trace...")
    r = requests.get(f"{BASE_URL}/workflows/{wf_id}/steps")
    assert r.status_code == 200
    steps = r.json()["steps"]
    assert len(steps) >= 4, f"expected the run to be persisted, got {len(steps)} steps"
    assert all(s["summary"] for s in steps), "persisted steps must carry a human-readable summary"
    print(f"[OK] {len(steps)} steps readable at /workflows/{wf_id[:8]}/steps")
    return steps


def test_proofs_reachable(wf_id):
    print("Checking proof artifacts...")
    r = requests.get(f"{BASE_URL}/workflows/{wf_id}/proofs")
    assert r.status_code == 200
    data = r.json()
    proofs = data["proofs"]
    kinds = {p["kind"] for p in proofs}
    assert "artifact" in kinds, f"no downloadable artifact in proofs: {kinds}"
    assert "crm_record" in kinds, f"no CRM proof in proofs: {kinds}"
    assert data["steps_ok"] == data["steps_total"], "proof summary disagrees with the step trace"
    for p in proofs:
        print(f"    proof {p['kind']:<13} {p['label']:<22} verified={p['verified']}")

    artifact = next(p for p in proofs if p["kind"] == "artifact")
    assert artifact["href"], "artifact proof has no download link"
    link = artifact["href"] if artifact["href"].startswith("http") else "http://localhost:8080" + artifact["href"]
    got = requests.get(link)
    assert got.status_code == 200, f"artifact not downloadable: {got.status_code}"
    assert len(got.content) > 0, "downloaded artifact is empty"
    print(f"[OK] Artifact downloadable ({len(got.content)} bytes) at {artifact['href']}")

    traversal = requests.get("http://localhost:8080/api/v1/artifacts/..%2F..%2Fconfig.yaml")
    assert traversal.status_code == 400, f"artifact route leaked a file: {traversal.status_code}"
    print("[OK] Artifact route refuses path traversal")
    return proofs


def test_crm(proofs):
    print("Verifying CRM record...")
    r = requests.get(f"{BASE_URL}/crm/customers/customer@demo.com")
    assert r.status_code == 200
    notes = r.json().get("notes", "")
    # Assert on the runtime-resolved attachment, not a hardcoded filename: the
    # workflow is address- and file-agnostic on purpose.
    assert "Attachment:" in notes, f"CRM note has no attachment reference: {notes}"
    assert "Email subject:" in notes, f"CRM note has no subject line: {notes}"
    referenced = notes.split("Attachment:")[1].strip().splitlines()[0].strip()
    filename = referenced.replace("\\", "/").split("/")[-1]
    print(f"    note references attachment: {filename}")

    # The referenced file must genuinely exist behind the artifact route.
    got = requests.get(f"http://localhost:8080/api/v1/artifacts/{filename}")
    assert got.status_code == 200, f"CRM-referenced file is not downloadable: {got.status_code}"
    assert len(got.content) > 0, "CRM-referenced file is empty"
    print(f"[OK] CRM record is consistent with a downloadable {len(got.content)}-byte artifact")


def main():
    print("=" * 58)
    print("WorkFlowOS End-to-End Test (observe -> detect -> approve -> prove)")
    print("=" * 58)
    try:
        test_health()
        inbox_target = test_ingest_events()
        seq_id = test_sequence_detection(inbox_target)
        wf_id = test_workflow_generation(seq_id)
        test_approval(wf_id)
        test_execution(wf_id)
        test_steps_persisted(wf_id)
        proofs = test_proofs_reachable(wf_id)
        test_crm(proofs)
        print("=" * 58)
        print("ALL TESTS PASSED - automation ran and its proofs are reachable")
        print("=" * 58)
    except AssertionError as e:
        print(f"[FAIL] TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[FAIL] ERROR: {e}")
        sys.exit(1)
if __name__ == "__main__":
    main()
