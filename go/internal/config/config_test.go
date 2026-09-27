package config

import (
	"os"
	"path/filepath"
	"testing"
)

// A .env written by PowerShell 5.1 or Notepad starts with a UTF-8 BOM. If that is
// not stripped, the first variable silently fails to load - which on Windows
// looks like "my Slack webhook is being ignored".
func TestLoadDotEnvStripsBOM(t *testing.T) {
	dir := t.TempDir()
	bom := "\xEF\xBB\xBF"
	content := bom + "SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T/B/W\n" +
		"# a comment\n" +
		"export LLM_API_KEY=\"quoted-key\"\n" +
		"ARTIFACT_DIR=../python/downloads\n"
	if err := os.WriteFile(filepath.Join(dir, ".env"), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}

	previous, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(dir); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.Chdir(previous) })

	values := loadDotEnv()

	if got := values["SLACK_WEBHOOK_URL"]; got != "https://hooks.slack.com/services/T/B/W" {
		t.Errorf("BOM broke the first key: got %q", got)
	}
	if got := values["LLM_API_KEY"]; got != "quoted-key" {
		t.Errorf("expected quotes stripped and export prefix handled, got %q", got)
	}
	if got := values["ARTIFACT_DIR"]; got != "../python/downloads" {
		t.Errorf("unexpected value %q", got)
	}
}

func TestRealEnvVarBeatsDotEnv(t *testing.T) {
	fixture, err := filepath.Abs("testdata/testdata.yaml")
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, ".env"), []byte("LLM_API_KEY=from-file\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	previous, _ := os.Getwd()
	if err := os.Chdir(dir); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.Chdir(previous) })
	t.Setenv("LLM_API_KEY", "from-shell")

	cfg, err := Load(fixture)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.LLM.APIKey != "from-shell" {
		t.Errorf("real env var must win, got %q", cfg.LLM.APIKey)
	}
	if cfg.LLM.BaseURL != "https://api.groq.com/openai/v1" {
		t.Errorf("yaml default should survive, got %q", cfg.LLM.BaseURL)
	}
}
