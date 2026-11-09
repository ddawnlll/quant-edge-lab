// Package ledger owns the append-only research ledger and SQLite migrations.
package ledger

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"github.com/ddawnlll/quant-edge-lab/contracts/go/qelcontract"

	_ "modernc.org/sqlite"
)

const schemaVersion = 1

// Event is the public ledger payload shape (runtime.schema.json#/$defs/LedgerEvent).
type Event struct {
	LedgerSeq      string  `json:"ledger_seq"`
	EventID        string  `json:"event_id"`
	RecordedAtNS   string  `json:"recorded_at_ns"`
	EffectiveAtNS  string  `json:"effective_at_ns"`
	ActorPrincipal string  `json:"actor_principal"`
	Role           string  `json:"role"`
	ToolVersion    string  `json:"tool_version"`
	ProgramID      *string `json:"program_id"`
	FamilyID       *string `json:"family_id"`
	HypothesisID   *string `json:"hypothesis_id"`
	RunKey         *string `json:"run_key"`
	AttemptID      *string `json:"attempt_id"`
	EventType      string  `json:"event_type"`
	SchemaVersion  string  `json:"schema_version"`
	PayloadHash    string  `json:"payload_hash"`
	ArtifactRefs   []any   `json:"artifact_refs"`
	PreviousHash   string  `json:"previous_hash"`
	EventHash      string  `json:"event_hash"`
	Supersedes     *string `json:"supersedes"`
	ReasonCode     *string `json:"reason_code"`
	Source         *string `json:"source"`
	RecordScope    string  `json:"record_scope"`
}

// Input is the caller-supplied event content before sequencing and hashing.
type Input struct {
	EffectiveAtNS  string
	ActorPrincipal string
	Role           string
	ToolVersion    string
	ProgramID      *string
	FamilyID       *string
	HypothesisID   *string
	RunKey         *string
	AttemptID      *string
	EventType      string
	Payload        map[string]any
	ArtifactRefs   []any
	Supersedes     *string
	ReasonCode     *string
	Source         *string
	RecordScope    string
}

type Ledger struct {
	db   *sql.DB
	path string
	mu   sync.Mutex
}

// LockForSeal serializes artifact publication with ledger appends.
func (l *Ledger) LockForSeal()   { l.mu.Lock() }
func (l *Ledger) UnlockForSeal() { l.mu.Unlock() }

func Open(path string) (*Ledger, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		return nil, err
	}
	dsn := "file:" + path + "?_pragma=journal_mode(WAL)&_pragma=synchronous(FULL)&_pragma=busy_timeout(5000)&_pragma=foreign_keys(ON)&_pragma=wal_autocheckpoint(16000)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	l := &Ledger{db: db, path: path}
	if err := l.migrate(); err != nil {
		db.Close()
		return nil, err
	}
	return l, nil
}

func (l *Ledger) Close() error { return l.db.Close() }

func (l *Ledger) DB() *sql.DB { return l.db }

func (l *Ledger) migrate() error {
	ctx := context.Background()
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS ledger_events (
			seq INTEGER PRIMARY KEY AUTOINCREMENT,
			event_id TEXT NOT NULL UNIQUE,
			event_type TEXT NOT NULL,
			recorded_at_ns TEXT NOT NULL,
			effective_at_ns TEXT NOT NULL,
			actor_principal TEXT NOT NULL,
			role TEXT NOT NULL,
			tool_version TEXT NOT NULL,
			payload_json TEXT NOT NULL,
			payload_hash TEXT NOT NULL,
			artifact_refs_json TEXT NOT NULL DEFAULT '[]',
			previous_hash TEXT NOT NULL,
			event_hash TEXT NOT NULL UNIQUE,
			program_id TEXT, family_id TEXT, hypothesis_id TEXT,
			run_key TEXT, attempt_id TEXT,
			supersedes TEXT, reason_code TEXT, source TEXT, record_scope TEXT NOT NULL
		)`,
		`CREATE INDEX IF NOT EXISTS idx_ledger_type ON ledger_events(event_type)`,
		`CREATE TABLE IF NOT EXISTS idempotency (
			key TEXT PRIMARY KEY,
			body_hash TEXT NOT NULL,
			receipt_json TEXT NOT NULL,
			created_ns TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS artifacts (
			artifact_id TEXT PRIMARY KEY,
			manifest_json TEXT NOT NULL,
			manifest_hash TEXT NOT NULL,
			event_hash TEXT NOT NULL DEFAULT '',
			created_ns TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS artifact_files (
			manifest_hash TEXT NOT NULL,
			path TEXT NOT NULL,
			sha256 TEXT NOT NULL,
			size_bytes INTEGER NOT NULL,
			PRIMARY KEY (manifest_hash, path)
		)`,
		`CREATE TABLE IF NOT EXISTS seal_failures (
			seq INTEGER PRIMARY KEY AUTOINCREMENT,
			artifact_id TEXT NOT NULL,
			point TEXT NOT NULL,
			message TEXT NOT NULL,
			usage_json TEXT NOT NULL,
			created_ns TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS jobs (
			job_id TEXT PRIMARY KEY,
			state TEXT NOT NULL,
			kind TEXT NOT NULL,
			program_id TEXT NOT NULL,
			family_id TEXT, hypothesis_id TEXT, trial_id TEXT,
			run_key TEXT NOT NULL,
			attempt_id TEXT NOT NULL,
			envelope_json TEXT NOT NULL,
			lease_generation INTEGER NOT NULL DEFAULT 0,
			lease_owner TEXT,
			lease_expires_ns TEXT,
			result_json TEXT,
			reason_code TEXT,
			financial_trial INTEGER NOT NULL DEFAULT 0,
			created_ns TEXT NOT NULL,
			updated_ns TEXT NOT NULL
		)`,
		`CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, created_ns)`,
		`CREATE TABLE IF NOT EXISTS job_events (
			seq INTEGER PRIMARY KEY AUTOINCREMENT,
			job_id TEXT NOT NULL,
			event TEXT NOT NULL,
			from_state TEXT NOT NULL,
			to_state TEXT NOT NULL,
			payload_json TEXT NOT NULL,
			ts_ns TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS trials (
			trial_id TEXT PRIMARY KEY,
			family_id TEXT NOT NULL,
			hypothesis_id TEXT NOT NULL,
			run_key TEXT NOT NULL,
			reserved_ns TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS memory_claims (
			claim_id TEXT PRIMARY KEY,
			text TEXT NOT NULL,
			ledger_event_hash TEXT NOT NULL,
			created_ns TEXT NOT NULL
		)`,
		`CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(claim_id UNINDEXED, text)`,
		`CREATE TABLE IF NOT EXISTS agent_tasks (
			task_id TEXT PRIMARY KEY,
			state TEXT NOT NULL,
			owner TEXT,
			generation INTEGER NOT NULL DEFAULT 0,
			lease_expires_ns TEXT,
			payload_json TEXT NOT NULL,
			created_ns TEXT NOT NULL,
			updated_ns TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS family_budget (
			family_id TEXT PRIMARY KEY,
			cpu_millicores INTEGER NOT NULL DEFAULT 0,
			paid_calls INTEGER NOT NULL DEFAULT 0,
			repairs INTEGER NOT NULL DEFAULT 0,
			checked_ns TEXT NOT NULL
		)`,
	}
	for _, stmt := range stmts {
		if _, err := l.db.ExecContext(ctx, stmt); err != nil {
			return fmt.Errorf("migrate: %w", err)
		}
	}
	// Forward migration for databases created before event hashes were stored.
	_, _ = l.db.ExecContext(ctx, `ALTER TABLE artifacts ADD COLUMN event_hash TEXT NOT NULL DEFAULT ''`)
	_, err := l.db.ExecContext(ctx, `INSERT INTO schema_meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO NOTHING`, strconv.Itoa(schemaVersion))
	return err
}

func nowNS() string { return strconv.FormatInt(time.Now().UnixNano(), 10) }

func (l *Ledger) LastHash(ctx context.Context, tx *sql.Tx) (string, int64, error) {
	var hash string
	var seq int64
	err := tx.QueryRowContext(ctx, `SELECT event_hash, seq FROM ledger_events ORDER BY seq DESC LIMIT 1`).Scan(&hash, &seq)
	if err == sql.ErrNoRows {
		return "0", 0, nil
	}
	if err != nil {
		return "", 0, err
	}
	return hash, seq, nil
}

// Append writes one event in a transaction. The caller may pass an existing
// transaction to combine effects atomically; otherwise a new one is opened.
func (l *Ledger) Append(ctx context.Context, in Input) (Event, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	tx, err := l.db.BeginTx(ctx, nil)
	if err != nil {
		return Event{}, err
	}
	event, err := l.AppendTx(ctx, tx, in)
	if err != nil {
		tx.Rollback()
		return Event{}, err
	}
	if err := tx.Commit(); err != nil {
		return Event{}, err
	}
	return event, nil
}

// AppendTx appends inside a caller-owned transaction.
func (l *Ledger) AppendTx(ctx context.Context, tx *sql.Tx, in Input) (Event, error) {
	previous, seq, err := l.LastHash(ctx, tx)
	if err != nil {
		return Event{}, err
	}
	payloadJSON, err := json.Marshal(in.Payload)
	if err != nil {
		return Event{}, err
	}
	payloadHash, err := qelcontract.HashJCS(in.Payload)
	if err != nil {
		return Event{}, err
	}
	recorded := nowNS()
	effective := in.EffectiveAtNS
	if effective == "" {
		effective = recorded
	}
	refs := in.ArtifactRefs
	if refs == nil {
		refs = []any{}
	}
	refsJSON, err := json.Marshal(refs)
	if err != nil {
		return Event{}, err
	}
	if in.RecordScope == "" {
		in.RecordScope = "OPERATIONS"
	}
	if in.ActorPrincipal == "" {
		in.ActorPrincipal = "P-LABD"
	}
	if in.Role == "" {
		in.Role = "P-LABD"
	}
	if in.ToolVersion == "" {
		in.ToolVersion = "qeld/0.1.0"
	}
	hashMaterial := map[string]any{
		"ledger_seq":      strconv.FormatInt(seq+1, 10),
		"recorded_at_ns":  recorded,
		"effective_at_ns": effective,
		"actor_principal": in.ActorPrincipal,
		"role":            in.Role,
		"tool_version":    in.ToolVersion,
		"program_id":      ptr(in.ProgramID),
		"family_id":       ptr(in.FamilyID),
		"hypothesis_id":   ptr(in.HypothesisID),
		"run_key":         ptr(in.RunKey),
		"attempt_id":      ptr(in.AttemptID),
		"event_type":      in.EventType,
		"schema_version":  "1.1.0",
		"payload_hash":    payloadHash,
		"artifact_refs":   refs,
		"previous_hash":   previous,
		"supersedes":      ptr(in.Supersedes),
		"reason_code":     ptr(in.ReasonCode),
		"source":          ptr(in.Source),
		"record_scope":    in.RecordScope,
	}
	eventHash, err := qelcontract.DomainHash("QEL.LEDGER.1.1\x00", hashMaterial)
	if err != nil {
		return Event{}, err
	}
	eventID, err := qelcontract.DomainHash("QEL.EVENT.1.1\x00", hashMaterial)
	if err != nil {
		return Event{}, err
	}
	ev := Event{
		LedgerSeq:      strconv.FormatInt(seq+1, 10),
		EventID:        eventID,
		RecordedAtNS:   recorded,
		EffectiveAtNS:  effective,
		ActorPrincipal: in.ActorPrincipal,
		Role:           in.Role,
		ToolVersion:    in.ToolVersion,
		ProgramID:      in.ProgramID,
		FamilyID:       in.FamilyID,
		HypothesisID:   in.HypothesisID,
		RunKey:         in.RunKey,
		AttemptID:      in.AttemptID,
		EventType:      in.EventType,
		SchemaVersion:  "1.1.0",
		PayloadHash:    payloadHash,
		ArtifactRefs:   refs,
		PreviousHash:   previous,
		EventHash:      eventHash,
		Supersedes:     in.Supersedes,
		ReasonCode:     in.ReasonCode,
		Source:         in.Source,
		RecordScope:    in.RecordScope,
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO ledger_events
		(event_id,event_type,recorded_at_ns,effective_at_ns,actor_principal,role,tool_version,payload_json,payload_hash,artifact_refs_json,
		 previous_hash,event_hash,program_id,family_id,hypothesis_id,run_key,attempt_id,supersedes,reason_code,source,record_scope)
		VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		ev.EventID, ev.EventType, ev.RecordedAtNS, ev.EffectiveAtNS, ev.ActorPrincipal, ev.Role, ev.ToolVersion,
		string(payloadJSON), ev.PayloadHash, string(refsJSON), ev.PreviousHash, ev.EventHash,
		ptr(ev.ProgramID), ptr(ev.FamilyID), ptr(ev.HypothesisID), ptr(ev.RunKey), ptr(ev.AttemptID),
		ptr(ev.Supersedes), ptr(ev.ReasonCode), ptr(ev.Source), ev.RecordScope)
	if err != nil {
		return Event{}, err
	}
	return ev, nil
}

func ptr[T any](v *T) any {
	if v == nil {
		return nil
	}
	return *v
}

// Idempotent runs fn once per key and caches the JSON receipt. Reusing the
// key with a different body hash is a protocol conflict.
func (l *Ledger) Idempotent(ctx context.Context, key, bodyHash string, fn func(tx *sql.Tx) (any, error)) (json.RawMessage, bool, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	tx, err := l.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, false, err
	}
	defer tx.Rollback()
	var storedHash, receipt string
	err = tx.QueryRowContext(ctx, `SELECT body_hash, receipt_json FROM idempotency WHERE key=?`, key).Scan(&storedHash, &receipt)
	if err == nil {
		if storedHash != bodyHash {
			return nil, false, &ConflictError{Key: key}
		}
		return json.RawMessage(receipt), true, nil
	}
	if err != sql.ErrNoRows {
		return nil, false, err
	}
	result, err := fn(tx)
	if err != nil {
		return nil, false, err
	}
	raw, err := json.Marshal(result)
	if err != nil {
		return nil, false, err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO idempotency(key,body_hash,receipt_json,created_ns) VALUES(?,?,?,?)`, key, bodyHash, string(raw), nowNS()); err != nil {
		return nil, false, err
	}
	if err := tx.Commit(); err != nil {
		return nil, false, err
	}
	return raw, false, nil
}

type ConflictError struct{ Key string }

func (e *ConflictError) Error() string { return "IDEMPOTENCY_CONFLICT: " + e.Key }

func (l *Ledger) Page(ctx context.Context, cursor string, limit int) ([]Event, string, bool, error) {
	if limit <= 0 || limit > 200 {
		limit = 200
	}
	after := int64(0)
	if cursor != "" {
		v, err := strconv.ParseInt(cursor, 10, 64)
		if err != nil || v < 0 {
			return nil, "", false, fmt.Errorf("invalid cursor")
		}
		after = v
	}
	rows, err := l.db.QueryContext(ctx, `SELECT seq,event_id,recorded_at_ns,effective_at_ns,actor_principal,role,tool_version,
		program_id,family_id,hypothesis_id,run_key,attempt_id,event_type,'1.1.0',payload_hash,artifact_refs_json,previous_hash,event_hash,
		supersedes,reason_code,source,record_scope
		FROM ledger_events WHERE seq > ? ORDER BY seq LIMIT ?`, after, limit+1)
	if err != nil {
		return nil, "", false, err
	}
	defer rows.Close()
	var events []Event
	for rows.Next() {
		ev, err := scanEvent(rows)
		if err != nil {
			return nil, "", false, err
		}
		events = append(events, ev)
	}
	hasMore := len(events) > limit
	if hasMore {
		events = events[:limit]
	}
	next := ""
	if hasMore && len(events) > 0 {
		next = events[len(events)-1].LedgerSeq
	}
	return events, next, hasMore, rows.Err()
}

func scanEvent(rows *sql.Rows) (Event, error) {
	var ev Event
	var program, family, hypothesis, runKey, attempt, supersedes, reason, source sql.NullString
	var refsRaw string
	err := rows.Scan(&ev.LedgerSeq, &ev.EventID, &ev.RecordedAtNS, &ev.EffectiveAtNS, &ev.ActorPrincipal,
		&ev.Role, &ev.ToolVersion, &program, &family, &hypothesis, &runKey, &attempt, &ev.EventType,
		&ev.SchemaVersion, &ev.PayloadHash, &refsRaw, &ev.PreviousHash, &ev.EventHash, &supersedes, &reason, &source, &ev.RecordScope)
	if err != nil {
		return ev, err
	}
	ev.ProgramID = nullPtr(program)
	ev.FamilyID = nullPtr(family)
	ev.HypothesisID = nullPtr(hypothesis)
	ev.RunKey = nullPtr(runKey)
	ev.AttemptID = nullPtr(attempt)
	ev.Supersedes = nullPtr(supersedes)
	ev.ReasonCode = nullPtr(reason)
	ev.Source = nullPtr(source)
	if err := json.Unmarshal([]byte(refsRaw), &ev.ArtifactRefs); err != nil {
		return ev, err
	}
	if ev.ArtifactRefs == nil {
		ev.ArtifactRefs = []any{}
	}
	return ev, nil
}

func nullPtr(n sql.NullString) *string {
	if !n.Valid {
		return nil
	}
	v := n.String
	return &v
}

// Payload returns the stored payload for one event hash.
func (l *Ledger) Payload(ctx context.Context, eventHash string) (map[string]any, error) {
	var raw string
	if err := l.db.QueryRowContext(ctx, `SELECT payload_json FROM ledger_events WHERE event_hash=?`, eventHash).Scan(&raw); err != nil {
		return nil, err
	}
	var payload map[string]any
	return payload, json.Unmarshal([]byte(raw), &payload)
}

// VerifyChain recomputes every previous/event hash and checks the sequence.
func (l *Ledger) VerifyChain(ctx context.Context) error {
	rows, err := l.db.QueryContext(ctx, `SELECT seq,event_id,previous_hash,event_hash,payload_hash,payload_json,recorded_at_ns,effective_at_ns,
		actor_principal,role,tool_version,program_id,family_id,hypothesis_id,run_key,attempt_id,event_type,artifact_refs_json,supersedes,reason_code,source,record_scope
		FROM ledger_events ORDER BY seq`)
	if err != nil {
		return err
	}
	defer rows.Close()
	previous := "0"
	wantSeq := 1
	for rows.Next() {
		var seq, eventID, prev, hash, payloadHash, payloadRaw, recorded, effective, actor, role, tool, eventType, scope, refsRaw string
		var program, family, hypothesis, runKey, attempt, supersedes, reason, source sql.NullString
		if err := rows.Scan(&seq, &eventID, &prev, &hash, &payloadHash, &payloadRaw, &recorded, &effective, &actor, &role, &tool,
			&program, &family, &hypothesis, &runKey, &attempt, &eventType, &refsRaw, &supersedes, &reason, &source, &scope); err != nil {
			return err
		}
		if prev != previous {
			return fmt.Errorf("chain break at seq %s", seq)
		}
		if seq != strconv.Itoa(wantSeq) {
			return fmt.Errorf("sequence gap at seq %s, want %d", seq, wantSeq)
		}
		wantSeq++
		var payload map[string]any
		if err := json.Unmarshal([]byte(payloadRaw), &payload); err != nil {
			return fmt.Errorf("payload decode at seq %s: %w", seq, err)
		}
		computedPayload, err := qelcontract.HashJCS(payload)
		if err != nil {
			return fmt.Errorf("payload hash at seq %s: %w", seq, err)
		}
		if computedPayload != payloadHash {
			return fmt.Errorf("payload content mismatch at seq %s", seq)
		}
		var refs []any
		if err := json.Unmarshal([]byte(refsRaw), &refs); err != nil {
			return err
		}
		if refs == nil {
			refs = []any{}
		}
		material := map[string]any{
			"ledger_seq": seq, "recorded_at_ns": recorded, "effective_at_ns": effective,
			"actor_principal": actor, "role": role, "tool_version": tool,
			"program_id": ptr(nullPtr(program)), "family_id": ptr(nullPtr(family)), "hypothesis_id": ptr(nullPtr(hypothesis)),
			"run_key": ptr(nullPtr(runKey)), "attempt_id": ptr(nullPtr(attempt)), "event_type": eventType,
			"schema_version": "1.1.0", "payload_hash": payloadHash, "artifact_refs": refs,
			"previous_hash": previous, "supersedes": ptr(nullPtr(supersedes)), "reason_code": ptr(nullPtr(reason)),
			"source": ptr(nullPtr(source)), "record_scope": scope,
		}
		recomputed, err := qelcontract.DomainHash("QEL.LEDGER.1.1\x00", material)
		if err != nil {
			return err
		}
		if recomputed != hash {
			return fmt.Errorf("hash mismatch at seq %s", seq)
		}
		recomputedID, err := qelcontract.DomainHash("QEL.EVENT.1.1\x00", material)
		if err != nil {
			return err
		}
		if recomputedID != eventID {
			return fmt.Errorf("event id mismatch at seq %s", seq)
		}
		previous = hash
	}
	return rows.Err()
}

// Checkpoint truncates the WAL after a full checkpoint.
func (l *Ledger) Checkpoint(ctx context.Context) error {
	_, err := l.db.ExecContext(ctx, `PRAGMA wal_checkpoint(TRUNCATE)`)
	return err
}

// Backup writes a consistent snapshot with VACUUM INTO.
func (l *Ledger) Backup(ctx context.Context, path string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		return err
	}
	_ = os.Remove(path)
	_, err := l.db.ExecContext(ctx, `VACUUM INTO ?`, path)
	return err
}

// SQLiteVersion returns the actual bundled SQLite version and source id.
func (l *Ledger) SQLiteVersion(ctx context.Context) (string, string, error) {
	var version, source string
	err := l.db.QueryRowContext(ctx, `SELECT sqlite_version(), sqlite_source_id()`).Scan(&version, &source)
	return version, source, err
}
