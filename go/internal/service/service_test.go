package service

import (
	"encoding/json"
	"path/filepath"
	"testing"

	"github.com/jmoiron/sqlx"
	_ "modernc.org/sqlite"

	"workflowos/internal/model"
)

func newTestDB(t *testing.T) *sqlx.DB {
	t.Helper()
	db, err := sqlx.Open("sqlite", filepath.Join(t.TempDir(), "test.db")+"?_pragma=foreign_keys(0)")
	if err != nil {
		t.Fatalf("open test db: %v", err)
	}
	db.SetMaxOpenConns(1)
	statements := []string{
		`CREATE TABLE workflows (id TEXT PRIMARY KEY, intent TEXT, trigger TEXT, actions TEXT, conditions TEXT, variables TEXT, status TEXT, sequence_id TEXT, created_at DATETIME, updated_at DATETIME, executed_at DATETIME)`,
		`CREATE TABLE execution_steps (id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT NOT NULL, step INTEGER NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '{}', duration_ms INTEGER NOT NULL DEFAULT 0, created_at DATETIME NOT NULL)`,
	}
	for _, statement := range statements {
		if _, err := db.Exec(statement); err != nil {
			t.Fatalf("schema: %v", err)
		}
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func insertWorkflow(t *testing.T, db *sqlx.DB, id string) {
	t.Helper()
	_, err := db.Exec(`INSERT INTO workflows (id,intent,trigger,actions,conditions,variables,status,sequence_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)`,
		id, "Process customer request", "{}", "[]", "[]", "{}", "approved", "seq-1", "2024-01-01", "2024-01-01")
	if err != nil {
		t.Fatalf("insert workflow: %v", err)
	}
}

func TestBeginExecutionStepsMarksRunning(t *testing.T) {
	db := newTestDB(t)
	insertWorkflow(t, db, "wf-1")
	svc := NewWorkflowService(db)

	actions := []model.ActionSpec{
		{Step: 1, Action: "download_attachment", Target: "gmail"},
		{Step: 2, Action: "lookup_customer", Target: "crm"},
	}
	if err := svc.BeginExecutionSteps("wf-1", actions); err != nil {
		t.Fatalf("BeginExecutionSteps: %v", err)
	}

	steps, err := svc.GetExecutionSteps("wf-1")
	if err != nil {
		t.Fatalf("GetExecutionSteps: %v", err)
	}
	if len(steps) != 2 {
		t.Fatalf("expected 2 running steps, got %d", len(steps))
	}
	for _, step := range steps {
		if step.Status != "running" {
			t.Errorf("step %d: expected status running, got %q", step.Step, step.Status)
		}
	}
}

// The core guarantee: a finished run replaces the optimistic "running" rows with
// the executor's real per-step outcomes, including evidence a judge can verify.
func TestReplaceExecutionStepsPersistsEvidence(t *testing.T) {
	db := newTestDB(t)
	insertWorkflow(t, db, "wf-1")
	svc := NewWorkflowService(db)

	if err := svc.BeginExecutionSteps("wf-1", []model.ActionSpec{{Step: 1, Action: "download_attachment", Target: "gmail"}}); err != nil {
		t.Fatalf("BeginExecutionSteps: %v", err)
	}

	trace := []model.ExecutionStep{
		{Step: 1, Action: "download_attachment", Target: "gmail", Status: "ok",
			Summary: "downloaded contract.pdf", Evidence: `{"filename":"contract.pdf","filepath":"./downloads/contract.pdf"}`, DurationMS: 12},
		{Step: 2, Action: "update_customer", Target: "crm", Status: "failed",
			Summary: "CRM note write failed", Evidence: `{"error":"boom"}`, DurationMS: 3},
	}
	if err := svc.ReplaceExecutionSteps("wf-1", trace); err != nil {
		t.Fatalf("ReplaceExecutionSteps: %v", err)
	}

	steps, err := svc.GetExecutionSteps("wf-1")
	if err != nil {
		t.Fatalf("GetExecutionSteps: %v", err)
	}
	if len(steps) != 2 {
		t.Fatalf("expected 2 steps after replace, got %d", len(steps))
	}
	if steps[0].Status != "ok" || steps[0].DurationMS != 12 {
		t.Errorf("unexpected step 1: %+v", steps[0])
	}
	if steps[1].Status != "failed" {
		t.Errorf("expected step 2 failed, got %q", steps[1].Status)
	}

	var evidence map[string]interface{}
	if err := json.Unmarshal([]byte(steps[0].Evidence), &evidence); err != nil {
		t.Fatalf("evidence is not valid JSON: %v", err)
	}
	if evidence["filename"] != "contract.pdf" {
		t.Errorf("evidence lost the artifact filename: %v", evidence)
	}
}

func TestReplaceExecutionStepsClearsPreviousRun(t *testing.T) {
	db := newTestDB(t)
	insertWorkflow(t, db, "wf-1")
	svc := NewWorkflowService(db)

	for i := 0; i < 2; i++ {
		if err := svc.ReplaceExecutionSteps("wf-1", []model.ExecutionStep{
			{Step: 1, Action: "lookup_customer", Target: "crm", Status: "ok"},
		}); err != nil {
			t.Fatalf("replace run %d: %v", i, err)
		}
	}

	steps, _ := svc.GetExecutionSteps("wf-1")
	if len(steps) != 1 {
		t.Fatalf("re-running must not accumulate rows, got %d", len(steps))
	}
}

func TestFailExecutionStepsOnlyTouchesRunning(t *testing.T) {
	db := newTestDB(t)
	insertWorkflow(t, db, "wf-1")
	svc := NewWorkflowService(db)

	if err := svc.ReplaceExecutionSteps("wf-1", []model.ExecutionStep{
		{Step: 1, Action: "download_attachment", Target: "gmail", Status: "ok", Summary: "done"},
		{Step: 2, Action: "send_slack_notification", Target: "slack", Status: "running", Summary: "queued"},
	}); err != nil {
		t.Fatalf("seed: %v", err)
	}

	if err := svc.FailExecutionSteps("wf-1", "executor unreachable"); err != nil {
		t.Fatalf("FailExecutionSteps: %v", err)
	}

	steps, _ := svc.GetExecutionSteps("wf-1")
	if steps[0].Status != "ok" {
		t.Errorf("completed step must stay ok, got %q", steps[0].Status)
	}
	if steps[1].Status != "failed" || steps[1].Summary != "executor unreachable" {
		t.Errorf("running step should be failed with reason, got %+v", steps[1])
	}
}
