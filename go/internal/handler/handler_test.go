package handler

import "testing"

func TestRedactPII(t *testing.T) {
	input := "email person@example.com and phone +1 (555) 123-4567"
	got := redactPII(input)
	if got != "email [redacted-email] and phone [redacted-phone]" {
		t.Fatalf("unexpected redaction: %q", got)
	}
}

func TestValidateEvent(t *testing.T) {
	if err := validateEvent(EventIngestRequest{Source: "gmail", Action: "open", Target: "email", SessionID: "session"}); err != nil {
		t.Fatalf("expected valid event: %v", err)
	}
	if err := validateEvent(EventIngestRequest{Source: "", Action: "open", Target: "email", SessionID: "session"}); err == nil {
		t.Fatal("expected validation failure for blank source")
	}
}
