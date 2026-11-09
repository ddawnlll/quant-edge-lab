package queue

import (
	"context"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/ddawnlll/quant-edge-lab/contracts/go/qelcontract"
	"github.com/ddawnlll/quant-edge-lab/labd/ledger"
)

func envelope(jobID, idem string, financial bool) map[string]any {
	env := map[string]any{
		"schema_version": "1.1.0",
		"job_id":         jobID,
		"kind":           "conformance",
		"program_id":     "PROG-1",
		"family_id":      nil,
		"hypothesis_id":  nil,
		"trial_id":       nil,
		"run_key":        fmt.Sprintf("%064x", 1),
		"attempt_id":     "ATT-1",
		"input_artifacts": []any{map[string]any{
			"sha256":         fmt.Sprintf("%064x", 2),
			"schema_id":      "urn:qel:test",
			"schema_version": "1.1.0",
			"size_bytes":     "1",
			"visibility":     "DEVELOPMENT",
			"media_type":     "application/json",
		}},
		"strategy_hash":         fmt.Sprintf("%064x", 3),
		"binary_hash":           fmt.Sprintf("%064x", 4),
		"environment_hash":      fmt.Sprintf("%064x", 5),
		"required_capabilities": []any{"CAP-TAKER-REPLAY"},
		"resource_limits": map[string]any{
			"memory_max_mib": 256, "memory_high_mib": 192, "cpu_millicores": 500,
			"pids_max": 64, "threads_max": 2, "fds_max": 256, "scratch_max_mib": 64,
			"output_max_mib": 64, "stdout_max_mib": 16, "wall_seconds": 60,
			"spill_max_mib": 0, "swap_max_mib": 0,
		},
		"deadline_ns": "999999999999999999",
		"seed_protocol": map[string]any{
			"algorithm": "HMAC_SHA256_COUNTER_V1", "root_seed_hex": fmt.Sprintf("%064x", 6),
			"labels": []any{"LATENCY"}, "replication_ids": []any{0},
			"draw_mapping": "U64_BE_REJECTION_MODULO", "latency_tape_hash": nil,
		},
		"output_schema_id": "urn:qel:test:out",
		"source":           "backtest",
		"idempotency_key":  idem,
		"lease_generation": 0,
		"financial_trial":  financial,
	}
	if financial {
		env["family_id"] = "FAM-1"
		env["hypothesis_id"] = "HYP-1"
		env["trial_id"] = "TRIAL-1"
	}
	return env
}

func setup(t *testing.T) *Queue {
	t.Helper()
	l, err := ledger.Open(filepath.Join(t.TempDir(), "research.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { l.Close() })
	return New(l)
}

func TestSubmitClaimResult(t *testing.T) {
	q := setup(t)
	ctx := context.Background()
	env := envelope("JOB-1", fmt.Sprintf("%064x", 10), false)
	if err := qelcontract.ValidateDef("JobEnvelope", env); err != nil {
		t.Fatalf("envelope: %v", err)
	}
	receipt, err := q.Submit(ctx, env)
	if err != nil {
		t.Fatal(err)
	}
	if receipt["state"] != "QUEUED" || receipt["generation"].(float64) != 0 {
		t.Fatalf("bad receipt %v", receipt)
	}
	again, err := q.Submit(ctx, env)
	if err != nil {
		t.Fatal(err)
	}
	if again["reused"] != true {
		t.Fatal("duplicate submit was not reused")
	}
	job, err := q.Claim(ctx, "worker-1", time.Minute)
	if err != nil || job == nil {
		t.Fatalf("claim: %v %v", err, job)
	}
	if job.Generation != 1 {
		t.Fatalf("generation %d", job.Generation)
	}
	if err := q.Transition(ctx, "JOB-1", "CHILD_VERIFIED_STARTED", 1); err != nil {
		t.Fatal(err)
	}
	result := map[string]any{
		"job_id": "JOB-1", "attempt_id": "ATT-1", "run_key": fmt.Sprintf("%064x", 1),
		"fencing_generation": float64(1), "status": "OUTPUT_SEALED", "reason_code": nil,
		"outputs": []any{}, "semantic_output_hash": nil, "usage_hash": fmt.Sprintf("%064x", 7),
		"source": "backtest",
	}
	committed, err := q.Result(ctx, result)
	if err != nil || !committed {
		t.Fatalf("result: %v %v", err, committed)
	}
	status, err := q.Status(ctx, "JOB-1")
	if err != nil {
		t.Fatal(err)
	}
	if status["state"] != "OUTPUT_SEALED" {
		t.Fatalf("status %v", status)
	}
	if err := q.L.VerifyChain(ctx); err != nil {
		t.Fatal(err)
	}
}

func TestStaleResultRejectedAndTrialNotDoubleReserved(t *testing.T) {
	q := setup(t)
	ctx := context.Background()
	env := envelope("JOB-2", fmt.Sprintf("%064x", 11), true)
	if _, err := q.Submit(ctx, env); err != nil {
		t.Fatal(err)
	}
	job, err := q.Claim(ctx, "worker-1", 2*time.Millisecond)
	if err != nil || job == nil {
		t.Fatal(err)
	}
	time.Sleep(5 * time.Millisecond)
	n, err := q.RequeueExpired(ctx)
	if err != nil || n != 1 {
		t.Fatalf("requeue %d %v", n, err)
	}
	job2, err := q.Claim(ctx, "worker-2", time.Minute)
	if err != nil || job2 == nil || job2.Generation != 2 {
		t.Fatalf("second claim %v %v", job2, err)
	}
	result := map[string]any{
		"job_id": "JOB-2", "attempt_id": "ATT-1", "run_key": fmt.Sprintf("%064x", 1),
		"fencing_generation": float64(1), "status": "OUTPUT_SEALED", "reason_code": nil,
		"outputs": []any{}, "semantic_output_hash": nil, "usage_hash": fmt.Sprintf("%064x", 7),
		"source": "backtest",
	}
	committed, err := q.Result(ctx, result)
	if err != nil {
		t.Fatal(err)
	}
	if committed {
		t.Fatal("stale result committed")
	}
	// A second submission with the same trial, different idempotency key,
	// must not reserve a second financial trial.
	env2 := envelope("JOB-3", fmt.Sprintf("%064x", 12), true)
	env2["run_key"] = fmt.Sprintf("%064x", 99)
	if _, err := q.Submit(ctx, env2); err != nil {
		t.Fatal(err)
	}
	var trials int
	if err := q.L.DB().QueryRowContext(ctx, `SELECT COUNT(*) FROM trials WHERE trial_id='TRIAL-1'`).Scan(&trials); err != nil {
		t.Fatal(err)
	}
	if trials != 1 {
		t.Fatalf("trial reserved %d times", trials)
	}
}
