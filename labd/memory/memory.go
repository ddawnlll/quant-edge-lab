// Package memory provides durable research claims and CAS agent-task ownership.
package memory

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strconv"
	"time"

	"github.com/ddawnlll/quant-edge-lab/labd/ledger"
)

type Store struct{ L *ledger.Ledger }

func New(l *ledger.Ledger) *Store { return &Store{L: l} }

func (s *Store) AddClaim(ctx context.Context, claimID, text, ledgerEventHash string) error {
	if claimID == "" || text == "" || len(ledgerEventHash) != 64 {
		return fmt.Errorf("invalid claim")
	}
	for _, r := range ledgerEventHash {
		if !((r >= '0' && r <= '9') || (r >= 'a' && r <= 'f')) {
			return fmt.Errorf("invalid event hash")
		}
	}
	tx, err := s.L.DB().BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `INSERT OR REPLACE INTO memory_claims(claim_id,text,ledger_event_hash,created_ns) VALUES(?,?,?,?)`,
		claimID, text, ledgerEventHash, strconv.FormatInt(time.Now().UnixNano(), 10)); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM memory_fts WHERE claim_id=?`, claimID); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO memory_fts(claim_id,text) VALUES(?,?)`, claimID, text); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Store) Search(ctx context.Context, query string, limit int) ([]map[string]any, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	rows, err := s.L.DB().QueryContext(ctx, `SELECT c.claim_id,c.text,c.ledger_event_hash
		FROM memory_fts f JOIN memory_claims c ON c.claim_id=f.claim_id
		WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?`, query, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var id, text, hash string
		if err := rows.Scan(&id, &text, &hash); err != nil {
			return nil, err
		}
		out = append(out, map[string]any{"claim_id": id, "text": text, "ledger_event_hash": hash})
	}
	return out, rows.Err()
}

func (s *Store) Rebuild(ctx context.Context) (int, error) {
	tx, err := s.L.DB().BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `DELETE FROM memory_fts`); err != nil {
		return 0, err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO memory_fts(claim_id,text) SELECT claim_id,text FROM memory_claims`); err != nil {
		return 0, err
	}
	var n int
	if err := tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM memory_claims`).Scan(&n); err != nil {
		return 0, err
	}
	return n, tx.Commit()
}

func (s *Store) AddTask(ctx context.Context, taskID string, payload map[string]any) error {
	raw, _ := json.Marshal(payload)
	now := strconv.FormatInt(time.Now().UnixNano(), 10)
	_, err := s.L.DB().ExecContext(ctx, `INSERT INTO agent_tasks(task_id,state,generation,payload_json,created_ns,updated_ns)
		VALUES(?,?,?,?,?,?) ON CONFLICT(task_id) DO NOTHING`, taskID, "QUEUED", 0, string(raw), now, now)
	return err
}

// Assign uses compare-and-swap ownership and rejects stale generations.
func (s *Store) Assign(ctx context.Context, taskID, owner string, ttl time.Duration) (int64, error) {
	tx, err := s.L.DB().BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	var state string
	var generation int64
	var expires sql.NullString
	err = tx.QueryRowContext(ctx, `SELECT state,generation,lease_expires_ns FROM agent_tasks WHERE task_id=?`, taskID).Scan(&state, &generation, &expires)
	if err != nil {
		return 0, err
	}
	now := time.Now()
	expired := expires.Valid && expires.String < strconv.FormatInt(now.UnixNano(), 10)
	if state != "QUEUED" && !expired {
		return 0, fmt.Errorf("STALE_ASSIGNMENT")
	}
	newGen := generation + 1
	newExpiry := strconv.FormatInt(now.Add(ttl).UnixNano(), 10)
	res, err := tx.ExecContext(ctx, `UPDATE agent_tasks SET state='LEASED',owner=?,generation=?,lease_expires_ns=?,updated_ns=?
		WHERE task_id=? AND generation=?`, owner, newGen, newExpiry, strconv.FormatInt(now.UnixNano(), 10), taskID, generation)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	if n != 1 {
		return 0, fmt.Errorf("STALE_ASSIGNMENT")
	}
	return newGen, tx.Commit()
}

func (s *Store) Complete(ctx context.Context, taskID, owner string, generation int64, status string) error {
	res, err := s.L.DB().ExecContext(ctx, `UPDATE agent_tasks SET state=?,updated_ns=? WHERE task_id=? AND owner=? AND generation=? AND state='LEASED'`,
		status, strconv.FormatInt(time.Now().UnixNano(), 10), taskID, owner, generation)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n != 1 {
		return fmt.Errorf("STALE_ASSIGNMENT")
	}
	return nil
}
