package db

import (
	"fmt"
	"log"

	"github.com/jmoiron/sqlx"
	_ "modernc.org/sqlite"
	"workflowos/internal/config"
)

var Schema = `
CREATE TABLE IF NOT EXISTS events (
	id TEXT PRIMARY KEY,
	timestamp DATETIME NOT NULL,
	source TEXT NOT NULL,
	action TEXT NOT NULL,
	target TEXT NOT NULL,
	payload TEXT,
	session_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session_timestamp ON events(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_source_action ON events(source, action);

CREATE TABLE IF NOT EXISTS sequences (
	id TEXT PRIMARY KEY,
	pattern TEXT NOT NULL UNIQUE,
	count INTEGER NOT NULL DEFAULT 1,
	first_seen DATETIME NOT NULL,
	last_seen DATETIME NOT NULL,
	is_candidate BOOLEAN NOT NULL DEFAULT 0,
	session_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS workflows (
	id TEXT PRIMARY KEY,
	intent TEXT NOT NULL,
	trigger TEXT NOT NULL,
	actions TEXT NOT NULL,
	conditions TEXT NOT NULL,
	variables TEXT NOT NULL,
	status TEXT NOT NULL DEFAULT 'pending',
	sequence_id TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	executed_at DATETIME,
	FOREIGN KEY (sequence_id) REFERENCES sequences(id)
);

CREATE TABLE IF NOT EXISTS customers (
	id TEXT PRIMARY KEY,
	email TEXT NOT NULL UNIQUE,
	name TEXT NOT NULL,
	company TEXT,
	notes TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email);

CREATE TABLE IF NOT EXISTS execution_steps (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	workflow_id TEXT NOT NULL,
	step INTEGER NOT NULL,
	action TEXT NOT NULL,
	target TEXT NOT NULL,
	status TEXT NOT NULL,
	summary TEXT NOT NULL DEFAULT '',
	evidence TEXT NOT NULL DEFAULT '{}',
	duration_ms INTEGER NOT NULL DEFAULT 0,
	created_at DATETIME NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_execution_steps_wf ON execution_steps(workflow_id, step);
`

// migrate adds columns to existing tables. CREATE TABLE IF NOT EXISTS silently
// leaves an already-created table untouched, so schema evolution needs an
// explicit, idempotent ALTER.
func migrate(db *sqlx.DB) error {
	steps := []struct {
		table  string
		column string
		decl   string
	}{
		{"sequences", "session_id", "TEXT NOT NULL DEFAULT ''"},
	}
	for _, step := range steps {
		// Ignore "duplicate column name": the migration is meant to be re-runnable.
		_, _ = db.Exec(fmt.Sprintf("ALTER TABLE %s ADD COLUMN %s %s", step.table, step.column, step.decl))
	}
	// Indexes come last: they can only be created once their column exists.
	indexes := []string{
		"CREATE INDEX IF NOT EXISTS idx_sequences_session ON sequences(session_id)",
	}
	for _, statement := range indexes {
		if _, err := db.Exec(statement); err != nil {
			return fmt.Errorf("index creation failed (%s): %w", statement, err)
		}
	}
	return nil
}

func Init(cfg *config.DatabaseConfig) (*sqlx.DB, error) {
	db, err := sqlx.Connect("sqlite", cfg.Path+"?_pragma=journal_mode(WAL)&_pragma=busy_timeout(30000)&_pragma=synchronous(NORMAL)")
	if err != nil {
		return nil, fmt.Errorf("failed to connect to database: %w", err)
	}

	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)

	if _, err := db.Exec(Schema); err != nil {
		return nil, fmt.Errorf("failed to initialize schema: %w", err)
	}
	if err := migrate(db); err != nil {
		return nil, fmt.Errorf("failed to migrate schema: %w", err)
	}

	// Verify anything the code depends on actually exists, so a stale database
	// fails at boot with a clear message instead of a confusing runtime error.
	for _, check := range []string{
		"SELECT session_id FROM sequences LIMIT 1",
		"SELECT workflow_id FROM execution_steps LIMIT 1",
	} {
		if _, err := db.Exec(check); err != nil {
			return nil, fmt.Errorf("database is missing required schema (%v): %w\n"+
				"delete %s to rebuild it, then re-run the demo seed", check, err, cfg.Path)
		}
	}

	log.Println("Database initialized successfully")
	return db, nil
}
