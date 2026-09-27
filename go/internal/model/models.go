package model

import (
	"encoding/json"
	"time"

	"github.com/google/uuid"
)

type Event struct {
	ID        string    `db:"id" json:"id"`
	Timestamp time.Time `db:"timestamp" json:"timestamp"`
	Source    string    `db:"source" json:"source"`
	Action    string    `db:"action" json:"action"`
	Target    string    `db:"target" json:"target"`
	Payload   string    `db:"payload" json:"payload"`
	SessionID string    `db:"session_id" json:"session_id"`
}

type Sequence struct {
	ID          string    `db:"id" json:"id"`
	Pattern     string    `db:"pattern" json:"pattern"`
	Count       int       `db:"count" json:"count"`
	FirstSeen   time.Time `db:"first_seen" json:"first_seen"`
	LastSeen    time.Time `db:"last_seen" json:"last_seen"`
	IsCandidate bool      `db:"is_candidate" json:"is_candidate"`
	SessionID   string    `db:"session_id" json:"session_id"`
}

type Workflow struct {
	ID         string     `db:"id" json:"id"`
	Intent     string     `db:"intent" json:"intent"`
	Trigger    string     `db:"trigger" json:"trigger"`
	Actions    string     `db:"actions" json:"actions"`
	Conditions string     `db:"conditions" json:"conditions"`
	Variables  string     `db:"variables" json:"variables"`
	Status     string     `db:"status" json:"status"` // pending, approved, rejected, executing, completed, failed
	SequenceID string     `db:"sequence_id" json:"sequence_id"`
	CreatedAt  time.Time  `db:"created_at" json:"created_at"`
	UpdatedAt  time.Time  `db:"updated_at" json:"updated_at"`
	ExecutedAt *time.Time `db:"executed_at" json:"executed_at,omitempty"`
}

type Customer struct {
	ID        string    `db:"id" json:"id"`
	Email     string    `db:"email" json:"email"`
	Name      string    `db:"name" json:"name"`
	Company   string    `db:"company" json:"company"`
	Notes     string    `db:"notes" json:"notes"`
	CreatedAt time.Time `db:"created_at" json:"created_at"`
	UpdatedAt time.Time `db:"updated_at" json:"updated_at"`
}

// ExecutionStep is the durable, per-action evidence record written by the
// executor. It is the difference between "the automation claims it ran" and
// "here is exactly what happened at each step, with the artifact it produced".
type ExecutionStep struct {
	ID         int64     `db:"id" json:"id"`
	WorkflowID string    `db:"workflow_id" json:"workflow_id"`
	Step       int       `db:"step" json:"step"`
	Action     string    `db:"action" json:"action"`
	Target     string    `db:"target" json:"target"`
	Status     string    `db:"status" json:"status"` // ok | failed
	Summary    string    `db:"summary" json:"summary"`
	Evidence   string    `db:"evidence" json:"-"`
	DurationMS int       `db:"duration_ms" json:"duration_ms"`
	CreatedAt  time.Time `db:"created_at" json:"created_at"`
}

// Proof is a single judge-verifiable artifact produced by a run.
type Proof struct {
	Kind     string `json:"kind"` // artifact | crm_record | notification
	Label    string `json:"label"`
	Detail   string `json:"detail"`
	Step     int    `json:"step"`
	Href     string `json:"href,omitempty"`
	Verified bool   `json:"verified"`
}

type WorkflowSpec struct {
	Intent     string                 `json:"intent"`
	Trigger    TriggerSpec            `json:"trigger"`
	Actions    []ActionSpec           `json:"actions"`
	Conditions []ConditionSpec        `json:"conditions"`
	Variables  map[string]interface{} `json:"variables"`
}

type TriggerSpec struct {
	Type   string `json:"type"`
	Source string `json:"source"`
}

type ActionSpec struct {
	Step   int                    `json:"step"`
	Action string                 `json:"action"`
	Target string                 `json:"target"`
	Params map[string]interface{} `json:"params"`
}

type ConditionSpec struct {
	If   string `json:"if"`
	Then string `json:"then"`
}

func NewEvent(source, action, target, payload, sessionID string) *Event {
	return &Event{
		ID:        uuid.New().String(),
		Timestamp: time.Now(),
		Source:    source,
		Action:    action,
		Target:    target,
		Payload:   payload,
		SessionID: sessionID,
	}
}

func NewSequence(pattern string) *Sequence {
	now := time.Now()
	return &Sequence{
		ID:          uuid.New().String(),
		Pattern:     pattern,
		Count:       1,
		FirstSeen:   now,
		LastSeen:    now,
		IsCandidate: false,
		SessionID:   "",
	}
}

func NewWorkflow(spec WorkflowSpec, sequenceID string) *Workflow {
	now := time.Now()
	return &Workflow{
		ID:         uuid.New().String(),
		Intent:     spec.Intent,
		Trigger:    toJSON(spec.Trigger),
		Actions:    toJSON(spec.Actions),
		Conditions: toJSON(spec.Conditions),
		Variables:  toJSON(spec.Variables),
		Status:     "pending",
		SequenceID: sequenceID,
		CreatedAt:  now,
		UpdatedAt:  now,
	}
}

func toJSON(v interface{}) string {
	data, err := json.Marshal(v)
	if err != nil {
		return "{}"
	}
	return string(data)
}
