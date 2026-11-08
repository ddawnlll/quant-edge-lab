// Package queue implements the durable job state machine and leases.
package queue

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strconv"
	"time"

	"github.com/ddawnlll/quant-edge-lab/contracts/go/qelcontract"
	"github.com/ddawnlll/quant-edge-lab/labd/ledger"
)

var allowed = map[string]map[string]string{
	"QUEUED":                 {"ADMIT": "LEASED", "NO_FRESH_OOS": "WAITING_FRESH_OOS", "ABORT": "ABORTED"},
	"LEASED":                 {"CHILD_VERIFIED_STARTED": "RUNNING", "ABORT": "ABORTED", "LEASE_LOST": "QUEUED"},
	"RUNNING":                {"OUTPUT_VALIDATED": "OUTPUT_SEALED", "PROCESS_FAILURE": "FAILED", "LIMIT_HIT": "RESOURCE_LIMIT", "DATA_MISSING": "WAITING_DATA", "ABORT": "ABORTED", "LEASE_LOST": "QUEUED"},
	"OUTPUT_SEALED":          {"AUDIT_DISPATCH": "AUDITING"},
	"AUDITING":               {"SIGNED_DECISION": "DECIDED", "AUDIT_FAILED": "FAILED"},
	"FAILED":                 {"EXACT_RETRY": "QUEUED"},
	"WAITING_DATA":           {"NEW_VERIFIED_DATA": "QUEUED"},
	"WAITING_FRESH_OOS":      {"NEW_WINDOW_RESERVED": "QUEUED"},
	"RESOURCE_LIMIT":         {"LIMIT_CHANGE_AUTHORIZED": "QUEUED"},
	"WAITING_COMPUTE_BUDGET": {"BUDGET_GRANTED": "QUEUED"},
	"WAITING_AUTHORIZATION":  {"AUTHORIZED": "QUEUED"},
}

type Queue struct {
	L *ledger.Ledger
}

func New(l *ledger.Ledger) *Queue { return &Queue{L: l} }

type Job struct {
	JobID        string          `json:"job_id"`
	State        string          `json:"state"`
	Generation   int64           `json:"generation"`
	AttemptID    string          `json:"attempt_id"`
	Envelope     json.RawMessage `json:"envelope"`
	Financial    bool            `json:"financial_trial"`
	ReasonCode   *string         `json:"reason_code"`
	LeaseExpires *string         `json:"lease_expires_ns"`
}

func bodyHash(v any) (string, error) { return qelcontract.HashJCS(v) }

// Submit validates a JobEnvelope and queues it exactly once per idempotency key.
func (q *Queue) Submit(ctx context.Context, env map[string]any) (map[string]any, error) {
	if err := qelcontract.ValidateDef("JobEnvelope", env); err != nil {
		return nil, err
	}
	key, _ := env["idempotency_key"].(string)
	bh, err := bodyHash(env)
	if err != nil {
		return nil, err
	}
	raw, reused, err := q.L.Idempotent(ctx, key, bh, func(tx *sql.Tx) (any, error) {
		jobID, _ := env["job_id"].(string)
		kind, _ := env["kind"].(string)
		program, _ := env["program_id"].(string)
		runKey, _ := env["run_key"].(string)
		attempt, _ := env["attempt_id"].(string)
		financial, _ := env["financial_trial"].(bool)
		now := strconv.FormatInt(time.Now().UnixNano(), 10)
		envJSON, err := json.Marshal(env)
		if err != nil {
			return nil, err
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO jobs(job_id,state,kind,program_id,family_id,hypothesis_id,trial_id,run_key,attempt_id,
			envelope_json,lease_generation,financial_trial,created_ns,updated_ns) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			jobID, "QUEUED", kind, program, str(env["family_id"]), str(env["hypothesis_id"]), str(env["trial_id"]),
			runKey, attempt, string(envJSON), 0, boolInt(financial), now, now)
		if err != nil {
			return nil, err
		}
		if financial {
			trialID, _ := env["trial_id"].(string)
			familyID, _ := env["family_id"].(string)
			hypothesisID, _ := env["hypothesis_id"].(string)
			if _, err := tx.ExecContext(ctx, `INSERT OR IGNORE INTO trials(trial_id,family_id,hypothesis_id,run_key,reserved_ns) VALUES(?,?,?,?,?)`,
				trialID, familyID, hypothesisID, runKey, now); err != nil {
				return nil, err
			}
		}
		ev, err := q.L.AppendTx(ctx, tx, ledger.Input{
			EventType:      "JobStateChanged",
			RunKey:         &runKey,
			AttemptID:      &attempt,
			Payload:        map[string]any{"job_id": jobID, "from": "NONE", "to": "QUEUED", "kind": kind},
			RecordScope:    "OPERATIONS",
			ActorPrincipal: "P-LABD",
			Role:           "P-LABD",
		})
		if err != nil {
			return nil, err
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO job_events(job_id,event,from_state,to_state,payload_json,ts_ns) VALUES(?,?,?,?,?,?)`,
			jobID, "SUBMIT", "NONE", "QUEUED", string(envJSON), now)
		if err != nil {
			return nil, err
		}
		return map[string]any{
			"request_id":        "REQ-" + upper(jobID),
			"resource_id":       jobID,
			"ledger_event_hash": ev.EventHash,
			"state":             "QUEUED",
			"generation":        0,
			"reused":            false,
		}, nil
	})
	if err != nil {
		return nil, err
	}
	var receipt map[string]any
	if err := json.Unmarshal(raw, &receipt); err != nil {
		return nil, err
	}
	receipt["reused"] = reused
	return receipt, nil
}

func str(v any) any {
	if v == nil {
		return nil
	}
	if s, ok := v.(string); ok {
		return s
	}
	return nil
}

func boolInt(v bool) int {
	if v {
		return 1
	}
	return 0
}

func upper(s string) string {
	out := make([]byte, len(s))
	for i := range s {
		c := s[i]
		if c >= 'a' && c <= 'z' {
			c -= 32
		}
		out[i] = c
	}
	return string(out)
}

// Claim leases the oldest queued job and returns one fencing generation.
func (q *Queue) Claim(ctx context.Context, owner string, ttl time.Duration) (*Job, error) {
	tx, err := q.L.DB().BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	j := &Job{}
	var envJSON, leaseExpires sql.NullString
	err = tx.QueryRowContext(ctx, `SELECT job_id,state,lease_generation,attempt_id,envelope_json,financial_trial,reason_code,lease_expires_ns
		FROM jobs WHERE state='QUEUED' ORDER BY created_ns LIMIT 1`).Scan(&j.JobID, &j.State, &j.Generation, &j.AttemptID, &envJSON, &j.Financial, &j.ReasonCode, &leaseExpires)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	j.Envelope = json.RawMessage(envJSON.String)
	newGen := j.Generation + 1
	expires := strconv.FormatInt(time.Now().Add(ttl).UnixNano(), 10)
	if _, err := tx.ExecContext(ctx, `UPDATE jobs SET state='LEASED',lease_generation=?,lease_owner=?,lease_expires_ns=?,updated_ns=? WHERE job_id=? AND state='QUEUED'`,
		newGen, owner, expires, now(), j.JobID); err != nil {
		return nil, err
	}
	if err := transitionEvent(ctx, q, tx, j.JobID, "ADMIT", "QUEUED", "LEASED", newGen); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	j.State = "LEASED"
	j.Generation = newGen
	j.LeaseExpires = &expires
	return j, nil
}

func now() string { return strconv.FormatInt(time.Now().UnixNano(), 10) }

func attemptID(tx *sql.Tx, ctx context.Context, jobID string) string {
	var attempt string
	_ = tx.QueryRowContext(ctx, `SELECT attempt_id FROM jobs WHERE job_id=?`, jobID).Scan(&attempt)
	if attempt == "" {
		attempt = "ATT-1"
	}
	return attempt
}

func transitionEvent(ctx context.Context, q *Queue, tx *sql.Tx, jobID, event, from, to string, generation int64) error {
	payload := map[string]any{"job_id": jobID, "event": event, "from": from, "to": to, "generation": strconv.FormatInt(generation, 10)}
	raw, _ := json.Marshal(payload)
	if _, err := tx.ExecContext(ctx, `INSERT INTO job_events(job_id,event,from_state,to_state,payload_json,ts_ns) VALUES(?,?,?,?,?,?)`,
		jobID, event, from, to, string(raw), now()); err != nil {
		return err
	}
	_, err := q.L.AppendTx(ctx, tx, ledger.Input{
		EventType:      "JobStateChanged",
		Payload:        payload,
		RecordScope:    "OPERATIONS",
		ActorPrincipal: "P-LABD",
		Role:           "P-LABD",
	})
	return err
}

// Transition moves a job through the declared state machine.
func (q *Queue) Transition(ctx context.Context, jobID, event string, generation int64) error {
	tx, err := q.L.DB().BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var state string
	var gen int64
	if err := tx.QueryRowContext(ctx, `SELECT state,lease_generation FROM jobs WHERE job_id=?`, jobID).Scan(&state, &gen); err != nil {
		return err
	}
	if gen != generation {
		return fmt.Errorf("STALE_GENERATION")
	}
	next, ok := allowed[state][event]
	if !ok {
		return fmt.Errorf("PROTOCOL_VIOLATION: unlisted transition %s/%s", state, event)
	}
	if _, err := tx.ExecContext(ctx, `UPDATE jobs SET state=?,updated_ns=? WHERE job_id=?`, next, now(), jobID); err != nil {
		return err
	}
	if err := transitionEvent(ctx, q, tx, jobID, event, state, next, generation); err != nil {
		return err
	}
	return tx.Commit()
}

// Result commits a worker result only at the current fencing generation.
func (q *Queue) Result(ctx context.Context, result map[string]any) (bool, error) {
	if err := qelcontract.ValidateDef("JobResult", result); err != nil {
		return false, err
	}
	jobID, _ := result["job_id"].(string)
	genFloat, _ := result["fencing_generation"].(float64)
	generation := int64(genFloat)
	tx, err := q.L.DB().BeginTx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	var state string
	var gen int64
	if err := tx.QueryRowContext(ctx, `SELECT state,lease_generation FROM jobs WHERE job_id=?`, jobID).Scan(&state, &gen); err != nil {
		return false, err
	}
	if generation != gen {
		// Stale results are rejected and never committed.
		return false, nil
	}
	raw, _ := json.Marshal(result)
	next, _ := result["status"].(string)
	eventFor := map[string]string{
		"OUTPUT_SEALED": "OUTPUT_VALIDATED", "FAILED": "PROCESS_FAILURE",
		"RESOURCE_LIMIT": "LIMIT_HIT", "WAITING_DATA": "DATA_MISSING",
		"ABORTED": "ABORT", "WAITING_FRESH_OOS": "NO_FRESH_OOS",
		"WAITING_COMPUTE_BUDGET": "WAIT_BUDGET", "WAITING_AUTHORIZATION": "WAIT_AUTH",
	}
	event, ok := eventFor[next]
	if !ok {
		return false, fmt.Errorf("PROTOCOL_VIOLATION: unknown result status %s", next)
	}
	if next != state {
		if _, ok := allowed[state][event]; !ok {
			return false, fmt.Errorf("PROTOCOL_VIOLATION: unlisted transition %s/%s", state, next)
		}
	}
	if _, err := tx.ExecContext(ctx, `UPDATE jobs SET state=?,result_json=?,reason_code=?,updated_ns=? WHERE job_id=?`,
		next, string(raw), str(result["reason_code"]), now(), jobID); err != nil {
		return false, err
	}
	if err := transitionEvent(ctx, q, tx, jobID, event, state, next, generation); err != nil {
		return false, err
	}
	return true, tx.Commit()
}

// RequeueExpired fences lost leases and makes the job claimable again.
func (q *Queue) RequeueExpired(ctx context.Context) (int, error) {
	tx, err := q.L.DB().BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	rows, err := tx.QueryContext(ctx, `SELECT job_id,lease_generation,state FROM jobs WHERE state IN ('LEASED','RUNNING') AND lease_expires_ns IS NOT NULL AND lease_expires_ns < ?`, now())
	if err != nil {
		return 0, err
	}
	var ids []string
	var gens []int64
	var states []string
	for rows.Next() {
		var id string
		var gen int64
		var state string
		if err := rows.Scan(&id, &gen, &state); err != nil {
			rows.Close()
			return 0, err
		}
		ids = append(ids, id)
		gens = append(gens, gen)
		states = append(states, state)
	}
	rows.Close()
	for i, id := range ids {
		newAttempt := fmt.Sprintf("%s-R%d", attemptID(tx, ctx, id), gens[i]+1)
		if _, err := tx.ExecContext(ctx, `UPDATE jobs SET state='QUEUED',attempt_id=?,lease_owner=NULL,lease_expires_ns=NULL,updated_ns=? WHERE job_id=?`, newAttempt, now(), id); err != nil {
			return 0, err
		}
		if err := transitionEvent(ctx, q, tx, id, "LEASE_LOST", states[i], "QUEUED", gens[i]); err != nil {
			return 0, err
		}
	}
	return len(ids), tx.Commit()
}

// Status returns a JobStatus payload.
func (q *Queue) Status(ctx context.Context, jobID string) (map[string]any, error) {
	var state, attempt string
	var generation int64
	var result, reason sql.NullString
	err := q.L.DB().QueryRowContext(ctx, `SELECT state,lease_generation,attempt_id,result_json,reason_code FROM jobs WHERE job_id=?`, jobID).
		Scan(&state, &generation, &attempt, &result, &reason)
	if err != nil {
		return nil, err
	}
	var usage any
	outputs := []any{}
	var reasonVal any
	if reason.Valid {
		reasonVal = reason.String
	}
	if result.Valid {
		var res map[string]any
		if err := json.Unmarshal([]byte(result.String), &res); err == nil {
			usage = res["usage"]
			if o, ok := res["outputs"].([]any); ok {
				outputs = o
			}
			if rc, ok := res["reason_code"]; ok && rc != nil {
				reasonVal = rc
			}
		}
	}
	status := map[string]any{
		"job_id":            jobID,
		"state":             state,
		"generation":        generation,
		"latest_attempt_id": attempt,
		"usage":             usage,
		"outputs":           outputs,
		"reason_code":       reasonVal,
	}
	if err := qelcontract.ValidateDef("JobStatus", status); err != nil {
		return nil, err
	}
	return status, nil
}
