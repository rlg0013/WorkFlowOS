package llm

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"workflowos/internal/config"
)

type Client struct {
	config *config.LLMConfig
	client *http.Client
}

type ChatRequest struct {
	Model          string          `json:"model"`
	Messages       []Message       `json:"messages"`
	Temperature    float64         `json:"temperature"`
	MaxTokens      int             `json:"max_tokens"`
	ResponseFormat *ResponseFormat `json:"response_format,omitempty"`
}

type Message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type ResponseFormat struct {
	Type string `json:"type"`
}

type ChatResponse struct {
	Choices []Choice  `json:"choices"`
	Error   *APIError `json:"error,omitempty"`
}

type Choice struct {
	Message Message `json:"message"`
}

type APIError struct {
	Message string `json:"message"`
	Type    string `json:"type"`
	Code    string `json:"code"`
}

func NewClient(cfg *config.LLMConfig) *Client {
	return &Client{
		config: cfg,
		client: &http.Client{Timeout: 60 * time.Second},
	}
}

func (c *Client) GenerateWorkflow(prompt string) (string, error) {
	if strings.TrimSpace(c.config.APIKey) == "" {
		return "", fmt.Errorf("LLM_API_KEY is not configured; using local workflow fallback")
	}
	req := ChatRequest{
		Model:       c.config.Model,
		Temperature: 0.1,
		MaxTokens:   2000,
		Messages: []Message{
			{Role: "system", Content: SystemPrompt},
			{Role: "user", Content: prompt},
		},
		ResponseFormat: &ResponseFormat{Type: "json_object"},
	}

	body, err := json.Marshal(req)
	if err != nil {
		return "", err
	}

	url := c.config.BaseURL + "/chat/completions"
	httpReq, err := http.NewRequest("POST", url, bytes.NewBuffer(body))
	if err != nil {
		return "", err
	}

	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+c.config.APIKey)

	resp, err := c.client.Do(httpReq)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	var chatResp ChatResponse
	if err := json.NewDecoder(resp.Body).Decode(&chatResp); err != nil {
		return "", err
	}

	if chatResp.Error != nil {
		return "", fmt.Errorf("API error: %s", chatResp.Error.Message)
	}

	if len(chatResp.Choices) == 0 {
		return "", fmt.Errorf("no response from LLM")
	}

	return chatResp.Choices[0].Message.Content, nil
}

const SystemPrompt = `You are a workflow automation expert. Given a sequence of observed user actions, analyze the intent and generate a structured automation workflow as JSON.

Return ONLY valid JSON matching this exact schema:
{
  "intent": "string",
  "trigger": {"type": "string", "source": "string"},
  "actions": [
    {"step": 1, "action": "string", "target": "string", "params": {}}
  ],
  "conditions": [{"if": "string", "then": "string"}],
  "variables": {"key": "value"}
}

Rules:
- intent: concise description of what the workflow accomplishes
- trigger: what starts the workflow
- actions: ordered steps with action type, target system, and parameters
- conditions: optional conditional branches
- Use only these action types: "download_attachment", "lookup_customer", "update_customer", "send_slack_notification", "open_url", "click", "type", "wait"
- Target systems: "gmail", "crm", "slack", "browser"
- Keep params minimal and JSON-serializable

CRITICAL - runtime variables:
A discovered workflow must work on FUTURE input, not replay one recorded run.
Never hardcode a concrete email address, filename, customer name or ID that you
saw in the observed sequence. Instead use these runtime placeholders, which the
executor substitutes with values read from the actual email at run time:
  {{sender_email}}    the From address of the matched email
  {{sender_name}}     the display name of the sender
  {{subject}}         the email subject line
  {{attachment_name}} the saved attachment filename
  {{message_id}}      the Gmail message id
Anything you cannot resolve at run time must use a placeholder rather than a literal.

Example for a "new customer email with attachment" task:
{
  "intent": "Process a new customer email and its attachment",
  "trigger": {"type": "new_unread_email_with_attachment", "source": "gmail"},
  "actions": [
    {"step": 1, "action": "download_attachment", "target": "gmail", "params": {"query": "is:unread has:attachment"}},
    {"step": 2, "action": "lookup_customer", "target": "crm", "params": {"email": "{{sender_email}}"}},
    {"step": 3, "action": "update_customer", "target": "crm", "params": {"email": "{{sender_email}}", "note": "Customer request received.", "subject": "{{subject}}"}},
    {"step": 4, "action": "send_slack_notification", "target": "slack", "params": {"text": "New email from {{sender_email}}: {{subject}} ({{attachment_name}})"}}
  ],
  "conditions": [{"if": "no unread email has an attachment", "then": "exit without side effects"}],
  "variables": {"sender_email": "resolved at run time", "subject": "resolved at run time", "attachment_name": "resolved at run time"}
}`
