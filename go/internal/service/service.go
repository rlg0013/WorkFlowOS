package service

import (
	"encoding/json"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/jmoiron/sqlx"
	"workflowos/internal/model"
)

type RepetitionDetector struct {
	db               *sqlx.DB
	mu               sync.Mutex
	lastDesktopEvent map[string]time.Time
}

func NewRepetitionDetector(db *sqlx.DB) *RepetitionDetector {
	return &RepetitionDetector{db: db, lastDesktopEvent: make(map[string]time.Time)}
}

func (d *RepetitionDetector) IngestEvent(event *model.Event) error {
	if d.isDebounced(event) {
		return nil
	}
	query := `INSERT INTO events (id, timestamp, source, action, target, payload, session_id) VALUES (?, ?, ?, ?, ?, ?, ?)`
	_, err := d.db.Exec(query, event.ID, event.Timestamp, event.Source, event.Action, event.Target, event.Payload, event.SessionID)
	if err != nil {
		return err
	}

	go d.detectSequences(event.SessionID)
	return nil
}

// isDebounced protects the local agent from click/scroll storms without
// suppressing semantic integration events such as Gmail or CRM updates.
func (d *RepetitionDetector) isDebounced(event *model.Event) bool {
	if event.Source != "desktop" {
		return false
	}
	key := event.Action + ":" + event.Target
	d.mu.Lock()
	defer d.mu.Unlock()
	last := d.lastDesktopEvent[key]
	d.lastDesktopEvent[key] = event.Timestamp
	return !last.IsZero() && event.Timestamp.Sub(last) < 250*time.Millisecond
}

func (d *RepetitionDetector) detectSequences(sessionID string) {
	events, err := d.getRecentEvents(sessionID, 20)
	if err != nil || len(events) < 4 {
		return
	}

	// Each incoming event closes at most three candidate windows. Re-counting every
	// historic window on every event made counts grow exponentially during a demo.
	for windowSize := 4; windowSize <= 6; windowSize++ {
		if len(events) < windowSize {
			continue
		}
		window := events[len(events)-windowSize:]
		d.recordSequence(d.patternFromEvents(window), sessionID)
	}
}

func (d *RepetitionDetector) getRecentEvents(sessionID string, limit int) ([]model.Event, error) {
	var events []model.Event
	query := `SELECT * FROM events WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?`
	err := d.db.Select(&events, query, sessionID, limit)
	for left, right := 0, len(events)-1; left < right; left, right = left+1, right-1 {
		events[left], events[right] = events[right], events[left]
	}
	return events, err
}

func (d *RepetitionDetector) patternFromEvents(events []model.Event) string {
	parts := make([]string, len(events))
	for i, e := range events {
		parts[i] = e.Source + ":" + e.Action + ":" + e.Target
	}
	return strings.Join(parts, "->")
}

func (d *RepetitionDetector) recordSequence(pattern, sessionID string) {
	var seq model.Sequence
	query := `SELECT * FROM sequences WHERE pattern = ?`
	err := d.db.Get(&seq, query, pattern)

	now := time.Now()
	if err != nil {
		seq = *model.NewSequence(pattern)
		seq.SessionID = sessionID
		insertQuery := `INSERT INTO sequences (id, pattern, count, first_seen, last_seen, is_candidate, session_id) VALUES (?, ?, ?, ?, ?, ?, ?)`
		_, err = d.db.Exec(insertQuery, seq.ID, seq.Pattern, seq.Count, seq.FirstSeen, seq.LastSeen, seq.IsCandidate, seq.SessionID)
		if err != nil {
			slog.Error("sequence insert failed", "error", err)
		}
		return
	}

	seq.Count++
	seq.LastSeen = now
	seq.IsCandidate = seq.Count >= 3
	// Keep the most recent session so a caller can scope a lookup to the run it
	// just seeded instead of matching a stale pattern from an earlier test.
	seq.SessionID = sessionID

	updateQuery := `UPDATE sequences SET count = ?, last_seen = ?, is_candidate = ?, session_id = ? WHERE id = ?`
	_, err = d.db.Exec(updateQuery, seq.Count, seq.LastSeen, seq.IsCandidate, seq.SessionID, seq.ID)
	if err != nil {
		slog.Error("sequence update failed", "error", err)
	}
}

func (d *RepetitionDetector) GetCandidateSequences() ([]model.Sequence, error) {
	return d.candidateSequences("")
}

// GetCandidateSequencesForSession narrows the candidate list to one observation
// session, so a live run cannot adopt a pattern promoted by an earlier test.
func (d *RepetitionDetector) GetCandidateSequencesForSession(sessionID string) ([]model.Sequence, error) {
	return d.candidateSequences(sessionID)
}

func (d *RepetitionDetector) candidateSequences(sessionID string) ([]model.Sequence, error) {
	var sequences []model.Sequence
	query := `SELECT * FROM sequences WHERE is_candidate = 1 AND count >= 3`
	args := []interface{}{}
	if sessionID != "" {
		query += ` AND session_id = ?`
		args = append(args, sessionID)
	}
	query += ` ORDER BY count DESC, last_seen DESC`
	err := d.db.Select(&sequences, query, args...)
	return sequences, err
}

func (d *RepetitionDetector) GetSequenceEvents(sequenceID string) ([]model.Event, error) {
	var seq model.Sequence
	if err := d.db.Get(&seq, `SELECT * FROM sequences WHERE id = ?`, sequenceID); err != nil {
		return nil, err
	}

	parts := strings.Split(seq.Pattern, "->")
	if len(parts) == 0 {
		return nil, nil
	}

	var events []model.Event
	placeholders := strings.Repeat("?,", len(parts))
	placeholders = placeholders[:len(placeholders)-1]
	// Return one matching occurrence in its original order. The old IN query mixed
	// every event that happened to share a step and made the LLM infer a workflow
	// from unrelated activity.
	query := `SELECT * FROM events WHERE session_id = (SELECT e.session_id FROM events e WHERE e.source || ':' || e.action || ':' || e.target = ? ORDER BY e.timestamp DESC LIMIT 1) ORDER BY timestamp ASC LIMIT ?`

	args := []interface{}{parts[0], len(parts)}

	err := d.db.Select(&events, query, args...)
	return events, err
}

type WorkflowService struct {
	db *sqlx.DB
}

func NewWorkflowService(db *sqlx.DB) *WorkflowService {
	return &WorkflowService{db: db}
}

func (s *WorkflowService) CreateWorkflow(spec model.WorkflowSpec, sequenceID string) (*model.Workflow, error) {
	wf := model.NewWorkflow(spec, sequenceID)

	query := `INSERT INTO workflows (id, intent, trigger, actions, conditions, variables, status, sequence_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
	_, err := s.db.Exec(query, wf.ID, wf.Intent, wf.Trigger, wf.Actions, wf.Conditions, wf.Variables, wf.Status, wf.SequenceID, wf.CreatedAt, wf.UpdatedAt)
	if err != nil {
		return nil, err
	}
	return wf, nil
}

func (s *WorkflowService) GetWorkflow(id string) (*model.Workflow, error) {
	var wf model.Workflow
	query := `SELECT * FROM workflows WHERE id = ?`
	err := s.db.Get(&wf, query, id)
	return &wf, err
}

func (s *WorkflowService) ListWorkflows(status string) ([]model.Workflow, error) {
	var workflows []model.Workflow
	query := `SELECT * FROM workflows`
	args := []interface{}{}
	if status != "" {
		query += ` WHERE status = ?`
		args = append(args, status)
	}
	query += ` ORDER BY created_at DESC`
	err := s.db.Select(&workflows, query, args...)
	return workflows, err
}

func (s *WorkflowService) UpdateWorkflowStatus(id, status string) error {
	query := `UPDATE workflows SET status = ?, updated_at = ? WHERE id = ?`
	_, err := s.db.Exec(query, status, time.Now(), id)
	if err != nil {
		return err
	}
	if status == "completed" || status == "failed" {
		execQuery := `UPDATE workflows SET executed_at = ? WHERE id = ?`
		_, err = s.db.Exec(execQuery, time.Now(), id)
	}
	return err
}

func (s *WorkflowService) GetWorkflowSpec(wf *model.Workflow) (model.WorkflowSpec, error) {
	var spec model.WorkflowSpec
	spec.Intent = wf.Intent

	if err := json.Unmarshal([]byte(wf.Trigger), &spec.Trigger); err != nil {
		spec.Trigger = model.TriggerSpec{}
	}
	if err := json.Unmarshal([]byte(wf.Actions), &spec.Actions); err != nil {
		spec.Actions = []model.ActionSpec{}
	}
	if err := json.Unmarshal([]byte(wf.Conditions), &spec.Conditions); err != nil {
		spec.Conditions = []model.ConditionSpec{}
	}
	if err := json.Unmarshal([]byte(wf.Variables), &spec.Variables); err != nil {
		spec.Variables = map[string]interface{}{}
	}
	return spec, nil
}

// ReplaceExecutionSteps clears any previous trace for the workflow and writes
// the authoritative per-step outcome reported by the executor.
func (s *WorkflowService) ReplaceExecutionSteps(workflowID string, steps []model.ExecutionStep) error {
	if _, err := s.db.Exec(`DELETE FROM execution_steps WHERE workflow_id = ?`, workflowID); err != nil {
		return err
	}
	for _, step := range steps {
		if step.CreatedAt.IsZero() {
			step.CreatedAt = time.Now()
		}
		if step.Status == "" {
			step.Status = "ok"
		}
		query := `INSERT INTO execution_steps (workflow_id, step, action, target, status, summary, evidence, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
		if _, err := s.db.Exec(query, workflowID, step.Step, step.Action, step.Target, step.Status, step.Summary, step.Evidence, step.DurationMS, step.CreatedAt); err != nil {
			return err
		}
	}
	return nil
}

// BeginExecutionSteps writes a 'running' row per planned action before the
// executor is called, so a polling client can watch the automation advance
// instead of staring at a spinner for the whole run.
func (s *WorkflowService) BeginExecutionSteps(workflowID string, actions []model.ActionSpec) error {
	if _, err := s.db.Exec(`DELETE FROM execution_steps WHERE workflow_id = ?`, workflowID); err != nil {
		return err
	}
	now := time.Now()
	for index, action := range actions {
		step := action.Step
		if step == 0 {
			step = index + 1
		}
		query := `INSERT INTO execution_steps (workflow_id, step, action, target, status, summary, evidence, duration_ms, created_at) VALUES (?, ?, ?, ?, 'running', 'queued', '{}', 0, ?)`
		if _, err := s.db.Exec(query, workflowID, step, action.Action, action.Target, now); err != nil {
			return err
		}
	}
	return nil
}

// FailExecutionSteps marks every still-running step as failed with a reason, so
// an unreachable executor never leaves the UI stuck on "running" forever.
func (s *WorkflowService) FailExecutionSteps(workflowID, reason string) error {
	_, err := s.db.Exec(
		`UPDATE execution_steps SET status = 'failed', summary = ? WHERE workflow_id = ? AND status = 'running'`,
		reason, workflowID)
	return err
}

func (s *WorkflowService) GetExecutionSteps(workflowID string) ([]model.ExecutionStep, error) {
	var steps []model.ExecutionStep
	query := `SELECT * FROM execution_steps WHERE workflow_id = ? ORDER BY step ASC, id ASC`
	err := s.db.Select(&steps, query, workflowID)
	return steps, err
}

type CRMService struct {
	db *sqlx.DB
}

func NewCRMService(db *sqlx.DB) *CRMService {
	return &CRMService{db: db}
}

func (s *CRMService) GetCustomerByEmail(email string) (*model.Customer, error) {
	var customer model.Customer
	query := `SELECT * FROM customers WHERE email = ?`
	err := s.db.Get(&customer, query, email)
	if err != nil {
		return nil, err
	}
	return &customer, nil
}

func (s *CRMService) CreateCustomer(customer *model.Customer) error {
	now := time.Now()
	customer.CreatedAt = now
	customer.UpdatedAt = now
	query := `INSERT INTO customers (id, email, name, company, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)`
	_, err := s.db.Exec(query, customer.ID, customer.Email, customer.Name, customer.Company, customer.Notes, customer.CreatedAt, customer.UpdatedAt)
	return err
}

func (s *CRMService) UpdateCustomer(customer *model.Customer) error {
	customer.UpdatedAt = time.Now()
	query := `UPDATE customers SET name = ?, company = ?, notes = ?, updated_at = ? WHERE id = ?`
	_, err := s.db.Exec(query, customer.Name, customer.Company, customer.Notes, customer.UpdatedAt, customer.ID)
	return err
}

func (s *CRMService) UpsertCustomer(email, name, company, notes string) (*model.Customer, error) {
	customer, err := s.GetCustomerByEmail(email)
	if err != nil {
		customer = &model.Customer{
			ID:      "cust_" + email,
			Email:   email,
			Name:    name,
			Company: company,
			Notes:   notes,
		}
		if err := s.CreateCustomer(customer); err != nil {
			return nil, err
		}
		return customer, nil
	}

	customer.Name = name
	customer.Company = company
	customer.Notes = notes
	if err := s.UpdateCustomer(customer); err != nil {
		return nil, err
	}
	return customer, nil
}

func (s *CRMService) AddNote(customerID, note string) error {
	customer, err := s.GetCustomerByEmail(customerID)
	if err != nil {
		return err
	}
	if customer.Notes != "" {
		customer.Notes += "\n"
	}
	customer.Notes += note
	return s.UpdateCustomer(customer)
}
