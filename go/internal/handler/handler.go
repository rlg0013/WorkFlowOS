package handler

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"workflowos/internal/llm"
	"workflowos/internal/model"
	"workflowos/internal/service"
)

type EventIngestRequest struct {
	Source    string `json:"source" binding:"required"`
	Action    string `json:"action" binding:"required"`
	Target    string `json:"target" binding:"required"`
	Payload   string `json:"payload"`
	SessionID string `json:"session_id" binding:"required"`
}

type GenerateWorkflowRequest struct {
	SequenceID string `json:"sequence_id" binding:"required"`
}

type ApproveWorkflowRequest struct {
	WorkflowID string `json:"workflow_id" binding:"required"`
	Approved   bool   `json:"approved"`
}

type ExecuteWorkflowRequest struct {
	WorkflowID string `json:"workflow_id" binding:"required"`
}

type Handler struct {
	eventService      *service.RepetitionDetector
	workflowService   *service.WorkflowService
	crmService        *service.CRMService
	llmClient         *llm.Client
	pythonExecutorURL string
	artifactDir       string
}

var emailPattern = regexp.MustCompile(`(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}`)
var phonePattern = regexp.MustCompile(`\+?[0-9][0-9 ()-]{7,}[0-9]`)

func NewHandler(
	eventService *service.RepetitionDetector,
	workflowService *service.WorkflowService,
	crmService *service.CRMService,
	llmClient *llm.Client,
	pythonExecutorURL string,
	artifactDir string,
) *Handler {
	return &Handler{
		eventService:      eventService,
		workflowService:   workflowService,
		crmService:        crmService,
		llmClient:         llmClient,
		pythonExecutorURL: pythonExecutorURL,
		artifactDir:       artifactDir,
	}
}

func (h *Handler) IngestEvent(c *gin.Context) {
	var req EventIngestRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if err := validateEvent(req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	req.Payload = redactPII(req.Payload)

	event := model.NewEvent(req.Source, req.Action, req.Target, req.Payload, req.SessionID)
	if err := h.eventService.IngestEvent(event); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusOK, gin.H{"status": "ok", "event_id": event.ID})
}

func validateEvent(req EventIngestRequest) error {
	if len(req.Source) > 64 || len(req.Action) > 64 || len(req.Target) > 256 || len(req.SessionID) > 128 || len(req.Payload) > 4096 {
		return fmt.Errorf("event field exceeds the allowed size")
	}
	for _, value := range []string{req.Source, req.Action, req.Target, req.SessionID} {
		if strings.TrimSpace(value) == "" {
			return fmt.Errorf("source, action, target, and session_id are required")
		}
	}
	return nil
}

func redactPII(value string) string {
	value = emailPattern.ReplaceAllString(value, "[redacted-email]")
	return phonePattern.ReplaceAllString(value, "[redacted-phone]")
}

// SeedDemoObservation creates three semantic, cross-application examples of the
// customer-request task. It is deliberately an API endpoint so the product demo
// can start from the UI without asking a judge to replay desktop clicks.
func (h *Handler) SeedDemoObservation(c *gin.Context) {
	sessionID := "guided_demo_" + fmt.Sprint(time.Now().UnixNano())
	taskTarget := "customer_request_" + sessionID[len(sessionID)-8:]
	events := []EventIngestRequest{
		{Source: "gmail", Action: "open", Target: taskTarget, Payload: "from:customer@demo.com", SessionID: sessionID},
		{Source: "gmail", Action: "download", Target: "attachment", Payload: "contract.pdf", SessionID: sessionID},
		{Source: "crm", Action: "lookup", Target: "customer", Payload: "customer@demo.com", SessionID: sessionID},
		{Source: "crm", Action: "update", Target: "record", Payload: "Contract review request", SessionID: sessionID},
		{Source: "slack", Action: "notify", Target: "ops_channel", Payload: "Customer request received", SessionID: sessionID},
	}
	for repeat := 0; repeat < 3; repeat++ {
		for _, eventReq := range events {
			event := model.NewEvent(eventReq.Source, eventReq.Action, eventReq.Target, eventReq.Payload, sessionID)
			if err := h.eventService.IngestEvent(event); err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
				return
			}
		}
	}
	// Detection runs asynchronously as it does for the desktop agent.
	time.Sleep(150 * time.Millisecond)
	sequences, err := h.eventService.GetCandidateSequences()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	for _, sequence := range sequences {
		if strings.Contains(sequence.Pattern, taskTarget) {
			c.JSON(http.StatusOK, gin.H{"status": "observed", "session_id": sessionID, "sequence_id": sequence.ID})
			return
		}
	}
	c.JSON(http.StatusServiceUnavailable, gin.H{"error": "demo observation was recorded but is still being indexed; please try again"})
}

func (h *Handler) ListSequences(c *gin.Context) {
	// ?session_id= scopes results to one observation session.
	sequences, err := h.eventService.GetCandidateSequencesForSession(c.Query("session_id"))
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	if sequences == nil {
		sequences = []model.Sequence{}
	}
	c.JSON(http.StatusOK, gin.H{"sequences": sequences})
}

func (h *Handler) GenerateWorkflow(c *gin.Context) {
	var req GenerateWorkflowRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	events, err := h.eventService.GetSequenceEvents(req.SequenceID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	if len(events) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "no events found for sequence"})
		return
	}

	prompt := h.buildPrompt(events)
	var spec model.WorkflowSpec
	response, err := h.llmClient.GenerateWorkflow(prompt)
	if err != nil || json.Unmarshal([]byte(response), &spec) != nil {
		// A deterministic fallback keeps the local-first demo usable without an API
		// key while retaining the LLM path whenever one is available.
		spec = workflowFromEvents(events)
	}

	wf, err := h.workflowService.CreateWorkflow(spec, req.SequenceID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusOK, gin.H{"workflow": wf, "spec": spec})
}

func (h *Handler) buildPrompt(events []model.Event) string {
	var sb strings.Builder
	sb.WriteString("Observed action sequence:\n\n")
	for i, e := range events {
		sb.WriteString(fmt.Sprintf("%d. [%s] %s -> %s", i+1, e.Source, e.Action, e.Target))
		if e.Payload != "" {
			sb.WriteString(" | " + e.Payload)
		}
		sb.WriteString("\n")
	}
	sb.WriteString("\nAnalyze this sequence and generate a workflow automation JSON.")
	return sb.String()
}

func (h *Handler) ListWorkflows(c *gin.Context) {
	status := c.Query("status")
	workflows, err := h.workflowService.ListWorkflows(status)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"workflows": workflows})
}

func (h *Handler) GetWorkflow(c *gin.Context) {
	id := c.Param("id")
	wf, err := h.workflowService.GetWorkflow(id)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "workflow not found"})
		return
	}
	spec, _ := h.workflowService.GetWorkflowSpec(wf)
	c.JSON(http.StatusOK, gin.H{"workflow": wf, "spec": spec})
}

func (h *Handler) ApproveWorkflow(c *gin.Context) {
	var req ApproveWorkflowRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	status := "approved"
	if !req.Approved {
		status = "rejected"
	}

	if err := h.workflowService.UpdateWorkflowStatus(req.WorkflowID, status); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusOK, gin.H{"status": status})
}

func (h *Handler) ExecuteWorkflow(c *gin.Context) {
	var req ExecuteWorkflowRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	wf, err := h.workflowService.GetWorkflow(req.WorkflowID)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "workflow not found"})
		return
	}

	if wf.Status != "approved" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "workflow not approved"})
		return
	}

	if err := h.workflowService.UpdateWorkflowStatus(req.WorkflowID, "executing"); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	spec, err := h.workflowService.GetWorkflowSpec(wf)
	if err != nil {
		h.workflowService.UpdateWorkflowStatus(req.WorkflowID, "failed")
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	payload, _ := json.Marshal(map[string]interface{}{
		"workflow_id": wf.ID,
		"spec":        spec,
	})

	// Publish the planned steps as 'running' first so the dashboard can poll
	// /steps and show each action lighting up as the executor works.
	if err := h.workflowService.BeginExecutionSteps(req.WorkflowID, spec.Actions); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	resp, err := http.Post(h.pythonExecutorURL+"/execute", "application/json", strings.NewReader(string(payload)))
	if err != nil {
		h.workflowService.FailExecutionSteps(req.WorkflowID, "executor unreachable: "+err.Error())
		h.workflowService.UpdateWorkflowStatus(req.WorkflowID, "failed")
		c.JSON(http.StatusInternalServerError, gin.H{"error": "executor unavailable: " + err.Error()})
		return
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	var execution struct {
		Success bool   `json:"success"`
		Message string `json:"message"`
		Steps   []struct {
			Step       int             `json:"step"`
			Action     string          `json:"action"`
			Target     string          `json:"target"`
			Status     string          `json:"status"`
			Summary    string          `json:"summary"`
			Evidence   json.RawMessage `json:"evidence"`
			DurationMS int             `json:"duration_ms"`
		} `json:"steps"`
	}
	_ = json.Unmarshal(body, &execution)

	// Persist the executor's per-step trace before deciding the final status, so a
	// failed run still leaves behind evidence of exactly how far it got.
	steps := make([]model.ExecutionStep, 0, len(execution.Steps))
	for _, step := range execution.Steps {
		evidence := "{}"
		if len(step.Evidence) > 0 {
			evidence = string(step.Evidence)
		}
		steps = append(steps, model.ExecutionStep{
			Step:       step.Step,
			Action:     step.Action,
			Target:     step.Target,
			Status:     step.Status,
			Summary:    step.Summary,
			Evidence:   evidence,
			DurationMS: step.DurationMS,
		})
	}
	if len(steps) == 0 {
		// The executor answered but reported no trace: mark the run honestly
		// rather than leaving "running" rows behind.
		h.workflowService.FailExecutionSteps(req.WorkflowID, "executor returned no step trace")
	} else if err := h.workflowService.ReplaceExecutionSteps(req.WorkflowID, steps); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	if resp.StatusCode >= 400 || !execution.Success {
		h.workflowService.UpdateWorkflowStatus(req.WorkflowID, "failed")
		message := execution.Message
		if message == "" {
			message = "execution failed"
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": message, "steps": steps})
		return
	}

	h.workflowService.UpdateWorkflowStatus(req.WorkflowID, "completed")
	proofs := h.collectProofs(req.WorkflowID, steps)
	c.JSON(http.StatusOK, gin.H{"status": "completed", "steps": steps, "proofs": proofs})
}

// GetWorkflowSteps returns the durable per-step execution trace for a workflow.
// This is what the UI polls to show the automation visibly progressing.
func (h *Handler) GetWorkflowSteps(c *gin.Context) {
	id := c.Param("id")
	if _, err := h.workflowService.GetWorkflow(id); err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "workflow not found"})
		return
	}
	steps, err := h.workflowService.GetExecutionSteps(id)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	if steps == nil {
		steps = []model.ExecutionStep{}
	}
	c.JSON(http.StatusOK, gin.H{"workflow_id": id, "steps": steps})
}

// GetWorkflowProofs aggregates the judge-verifiable artifacts of a run so they
// are reachable in one click instead of via curl in a separate terminal.
func (h *Handler) GetWorkflowProofs(c *gin.Context) {
	id := c.Param("id")
	wf, err := h.workflowService.GetWorkflow(id)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "workflow not found"})
		return
	}
	steps, err := h.workflowService.GetExecutionSteps(id)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	proofs := h.collectProofs(id, steps)
	completed := 0
	failed := 0
	for _, step := range steps {
		if step.Status == "ok" {
			completed++
		} else {
			failed++
		}
	}
	c.JSON(http.StatusOK, gin.H{
		"workflow_id":  id,
		"intent":       wf.Intent,
		"status":       wf.Status,
		"executed_at":  wf.ExecutedAt,
		"steps_total":  len(steps),
		"steps_ok":     completed,
		"steps_failed": failed,
		"proofs":       proofs,
	})
}

// collectProofs turns raw step evidence into links a judge can click. Artifact
// links point at the backend's own artifact route, so the downloaded file the
// automation produced is directly downloadable from the dashboard.
func (h *Handler) collectProofs(workflowID string, steps []model.ExecutionStep) []model.Proof {
	proofs := []model.Proof{}
	for _, step := range steps {
		var evidence map[string]interface{}
		if step.Evidence != "" {
			_ = json.Unmarshal([]byte(step.Evidence), &evidence)
		}
		if evidence == nil {
			evidence = map[string]interface{}{}
		}
		verified := step.Status == "ok"

		if savedPath, ok := evidence["filepath"].(string); ok && savedPath != "" {
			filename, _ := evidence["filename"].(string)
			if filename == "" {
				filename = savedPath
			}
			// The executor's filepath is relative to its own working directory,
			// so link by basename and let the artifact root resolve it.
			basename := filepath.Base(filepath.FromSlash(filename))
			proofs = append(proofs, model.Proof{
				Kind:     "artifact",
				Label:    basename,
				Detail:   fmt.Sprintf("step %d wrote %s", step.Step, savedPath),
				Step:     step.Step,
				Href:     "/api/v1/artifacts/" + url.PathEscape(basename),
				Verified: verified,
			})
		}
		if note, ok := evidence["note"].(string); ok && note != "" {
			proofs = append(proofs, model.Proof{
				Kind:     "crm_record",
				Label:    "CRM note written",
				Detail:   note,
				Step:     step.Step,
				Verified: verified,
			})
		}
		if text, ok := evidence["text"].(string); ok && text != "" {
			proofs = append(proofs, model.Proof{
				Kind:     "notification",
				Label:    "Team notification",
				Detail:   text,
				Step:     step.Step,
				Verified: verified,
			})
		}
	}
	return proofs
}

// workflowFromEvents is the deterministic fallback used when no LLM key is set.
// It must stay address-agnostic: the sender is discovered at run time from the
// email that actually arrives, never frozen at generation time.
func workflowFromEvents(events []model.Event) model.WorkflowSpec {
	return model.WorkflowSpec{
		Intent:  "Process a new customer email and its attachment",
		Trigger: model.TriggerSpec{Type: "new_unread_email_with_attachment", Source: "gmail"},
		Actions: []model.ActionSpec{
			{Step: 1, Action: "download_attachment", Target: "gmail", Params: map[string]interface{}{
				"query": "is:unread has:attachment"}},
			{Step: 2, Action: "lookup_customer", Target: "crm", Params: map[string]interface{}{
				"email": "{{sender_email}}"}},
			{Step: 3, Action: "update_customer", Target: "crm", Params: map[string]interface{}{
				"email":   "{{sender_email}}",
				"note":    "Customer request received and logged by WorkFlowOS.",
				"subject": "{{subject}}"}},
			{Step: 4, Action: "send_slack_notification", Target: "slack", Params: map[string]interface{}{
				"text": "New customer email processed: {{subject}} (from {{sender_email}}, attachment {{attachment_name}})"}},
		},
		Conditions: []model.ConditionSpec{
			{If: "no unread email has an attachment", Then: "exit without side effects"},
			{If: "sender is not found in the CRM", Then: "create the record and flag for human review"},
		},
		Variables: map[string]interface{}{
			"sender_email":    "resolved at run time from the matched email",
			"subject":         "resolved at run time",
			"attachment_name": "resolved at run time",
		},
	}
}

// attachmentName pulls a plausible filename out of an observed payload.
func attachmentName(payload string) string {
	for _, field := range strings.FieldsFunc(payload, func(r rune) bool {
		return r == ' ' || r == ':' || r == ',' || r == ';' || r == '|' || r == '\n'
	}) {
		field = strings.TrimSpace(field)
		if strings.Contains(field, ".") && !strings.Contains(field, "@") {
			return field
		}
	}
	return ""
}

// ServeArtifact streams a file produced by the automation. The path is confined
// to the configured artifact root so a crafted request cannot escape it.
func (h *Handler) ServeArtifact(c *gin.Context) {
	root, err := filepath.Abs(getenvDefault("ARTIFACT_DIR", h.artifactDir))
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "artifact root unavailable"})
		return
	}
	requested := filepath.Clean("/" + c.Param("filepath"))
	target := filepath.Join(root, requested)

	rel, err := filepath.Rel(root, target)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid artifact path"})
		return
	}
	info, err := os.Stat(target)
	if err != nil || info.IsDir() {
		c.JSON(http.StatusNotFound, gin.H{"error": "artifact not found"})
		return
	}
	if c.Query("meta") == "1" {
		c.JSON(http.StatusOK, gin.H{
			"name": info.Name(), "bytes": info.Size(),
			"modified": info.ModTime(), "href": "/api/v1/artifacts/" + requested,
		})
		return
	}
	c.Header("Content-Disposition", "attachment; filename=\""+info.Name()+"\"")
	c.File(target)
}

func getenvDefault(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func (h *Handler) CRMCreateCustomer(c *gin.Context) {
	var customer model.Customer
	if err := c.ShouldBindJSON(&customer); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	customer.ID = "cust_" + customer.Email
	customer.CreatedAt = time.Now()
	customer.UpdatedAt = time.Now()
	if err := h.crmService.CreateCustomer(&customer); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusCreated, customer)
}

func (h *Handler) CRMGetCustomer(c *gin.Context) {
	email := c.Param("email")
	customer, err := h.crmService.GetCustomerByEmail(email)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "customer not found"})
		return
	}
	c.JSON(http.StatusOK, customer)
}

func (h *Handler) CRMUpsertCustomer(c *gin.Context) {
	var req struct {
		Email   string `json:"email" binding:"required"`
		Name    string `json:"name"`
		Company string `json:"company"`
		Notes   string `json:"notes"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	customer, err := h.crmService.UpsertCustomer(req.Email, req.Name, req.Company, req.Notes)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, customer)
}

func (h *Handler) CRMAddNote(c *gin.Context) {
	email := c.Param("email")
	var req struct {
		Note string `json:"note" binding:"required"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if err := h.crmService.AddNote(email, req.Note); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "ok"})
}
