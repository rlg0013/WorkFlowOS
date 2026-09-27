package config

import (
	"bufio"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

type Config struct {
	Server    ServerConfig    `yaml:"server"`
	Database  DatabaseConfig  `yaml:"database"`
	Artifacts ArtifactsConfig `yaml:"artifacts"`
	LLM       LLMConfig       `yaml:"llm"`
	Agent     AgentConfig     `yaml:"agent"`
	CRM       CRMConfig       `yaml:"crm"`
	Slack     SlackConfig     `yaml:"slack"`
	Gmail     GmailConfig     `yaml:"gmail"`
}

type ServerConfig struct {
	Port int `yaml:"port"`
}

type DatabaseConfig struct {
	Path string `yaml:"path"`
}

// ArtifactsConfig points at the directory the Python executor writes into.
type ArtifactsConfig struct {
	Dir string `yaml:"dir"`
}

type LLMConfig struct {
	BaseURL string `yaml:"base_url"`
	Model   string `yaml:"model"`
	APIKey  string `yaml:"api_key"`
}

type AgentConfig struct {
	Endpoint   string `yaml:"endpoint"`
	IntervalMs int    `yaml:"interval_ms"`
}

type CRMConfig struct {
	Port int `yaml:"port"`
}

type SlackConfig struct {
	WebhookURL string `yaml:"webhook_url"`
}

type GmailConfig struct {
	CredentialsFile string `yaml:"credentials_file"`
	TokenFile       string `yaml:"token_file"`
}

// loadDotEnv reads a .env file without adding a dependency. It looks in the
// working directory and one level up, so it works whether the server is started
// from go/ or from the repository root. Real environment variables always win.
func loadDotEnv() map[string]string {
	values := map[string]string{}
	candidates := []string{".env", filepath.Join("..", ".env")}
	for _, path := range candidates {
		file, err := os.Open(path)
		if err != nil {
			continue
		}
		scanner := bufio.NewScanner(file)
		for scanner.Scan() {
			line := strings.TrimSpace(scanner.Text())
			// Windows editors (and PowerShell 5.1 Set-Content) prepend a UTF-8
			// BOM, which would otherwise corrupt the first key's name.
			line = strings.TrimPrefix(line, "\uFEFF")
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			line = strings.TrimPrefix(line, "export ")
			key, value, found := strings.Cut(line, "=")
			if !found {
				continue
			}
			key = strings.TrimSpace(key)
			value = strings.TrimSpace(value)
			if len(value) >= 2 && (value[0] == '"' && value[len(value)-1] == '"' ||
				value[0] == '\'' && value[len(value)-1] == '\'') {
				value = value[1 : len(value)-1]
			}
			if key != "" {
				values[key] = value
			}
		}
		file.Close()
		break // nearest .env wins
	}
	return values
}

func firstNonEmpty(sources []string) string {
	for _, value := range sources {
		if value != "" {
			return value
		}
	}
	return ""
}

func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}

	// Precedence: real environment variable > .env file > config.yaml default.
	dotenv := loadDotEnv()
	pick := func(key string, current string) string {
		if fromOS := os.Getenv(key); fromOS != "" {
			return fromOS
		}
		if fromFile, ok := dotenv[key]; ok {
			return fromFile
		}
		return current
	}

	cfg.LLM.APIKey = pick("LLM_API_KEY", cfg.LLM.APIKey)
	cfg.Slack.WebhookURL = pick("SLACK_WEBHOOK_URL", cfg.Slack.WebhookURL)
	cfg.Artifacts.Dir = pick("ARTIFACT_DIR", cfg.Artifacts.Dir)
	cfg.LLM.BaseURL = pick("LLM_BASE_URL", cfg.LLM.BaseURL)
	cfg.LLM.Model = pick("LLM_MODEL", cfg.LLM.Model)
	if cfg.Artifacts.Dir == "" {
		cfg.Artifacts.Dir = firstNonEmpty([]string{"./downloads", "../python/downloads"})
	}

	return &cfg, nil
}
