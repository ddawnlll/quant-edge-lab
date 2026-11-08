// Package worker runs durable jobs in bounded subprocesses.
package worker

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/ddawnlll/quant-edge-lab/labd/artifacts"
	"github.com/ddawnlll/quant-edge-lab/labd/ledger"
	"github.com/ddawnlll/quant-edge-lab/labd/queue"
)

type Worker struct {
	Q    *queue.Queue
	A    *artifacts.Store
	L    *ledger.Ledger
	Root string
	Bin  string
}

func New(root string, q *queue.Queue, a *artifacts.Store, l *ledger.Ledger) *Worker {
	return &Worker{Q: q, A: a, L: l, Root: root, Bin: filepath.Join(root, "..", "bin")}
}

func (w *Worker) Run(ctx context.Context, owner string) error {
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
		job, err := w.Q.Claim(ctx, owner, 90*time.Second)
		if err != nil {
			return err
		}
		if job == nil {
			continue
		}
		if err := w.Q.Transition(ctx, job.JobID, "CHILD_VERIFIED_STARTED", job.Generation); err != nil {
			return err
		}
		w.execute(ctx, job)
	}
}

func (w *Worker) execute(ctx context.Context, job *queue.Job) {
	started := time.Now()
	jobDir := filepath.Join(w.Root, "jobs", job.JobID)
	_ = os.MkdirAll(jobDir, 0o750)
	var env map[string]any
	if err := json.Unmarshal(job.Envelope, &env); err != nil {
		w.fail(ctx, job, "SCHEMA_INVALID", err, started)
		return
	}
	params, err := w.params(ctx, env)
	if err != nil {
		w.fail(ctx, job, "ARTIFACT_INCOMPLETE", err, started)
		return
	}
	kind, _ := env["kind"].(string)
	outPath := filepath.Join(jobDir, "report.json")
	limit := 1800 * time.Second
	if rl, ok := env["resource_limits"].(map[string]any); ok {
		if secs, ok := rl["wall_seconds"].(float64); ok && secs > 0 {
			limit = time.Duration(secs) * time.Second
		}
	}
	runCtx, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	cmd, err := w.command(runCtx, kind, params, outPath, jobDir)
	if err != nil {
		w.fail(ctx, job, "UNSUPPORTED_CAPABILITY", err, started)
		return
	}
	stdout, _ := os.Create(filepath.Join(jobDir, "stdout.log"))
	stderr, _ := os.Create(filepath.Join(jobDir, "stderr.log"))
	defer stdout.Close()
	defer stderr.Close()
	cmd.Stdout = &limitedWriter{w: stdout, n: 16 << 20}
	cmd.Stderr = &limitedWriter{w: stderr, n: 16 << 20}
	cmd.Env = append(os.Environ(), "PYTHONPATH=python", "QEL_ROOT="+abs(filepath.Join(w.Root, "..")))
	err = cmd.Run()
	if err != nil {
		code := "PROCESS_FAILURE"
		if runCtx.Err() == context.DeadlineExceeded {
			code = "RESOURCE_LIMIT"
		}
		w.fail(ctx, job, code, err, started)
		return
	}
	report, err := os.ReadFile(outPath)
	if err != nil {
		w.fail(ctx, job, "ARTIFACT_INCOMPLETE", err, started)
		return
	}
	usage := map[string]any{
		"attempt_id":        job.AttemptID,
		"wall_ns":           strconv.FormatInt(time.Since(started).Nanoseconds(), 10),
		"cpu_ns":            "0",
		"cgroup_peak_bytes": "0",
		"rss_peak_bytes":    "0",
		"pss_peak_bytes":    nil,
		"read_bytes":        "0",
		"written_bytes":     strconv.Itoa(len(report)),
	}
	usageHash, _ := hashAny(usage)
	ref, err := w.seal(ctx, job, report, usage)
	if err != nil {
		w.fail(ctx, job, "ARTIFACT_INCOMPLETE", err, started)
		return
	}
	result := map[string]any{
		"job_id": job.JobID, "attempt_id": job.AttemptID,
		"run_key": value(env, "run_key"), "fencing_generation": float64(job.Generation),
		"status": "OUTPUT_SEALED", "reason_code": nil, "outputs": []any{ref},
		"semantic_output_hash": ref["sha256"], "usage_hash": usageHash,
		"source": value(env, "source"),
	}
	committed, err := w.Q.Result(ctx, result)
	if err != nil || !committed {
		w.L.Append(ctx, ledger.Input{EventType: "RunAborted", Payload: map[string]any{"job_id": job.JobID, "error": fmt.Sprint(err)}})
	}
}

func (w *Worker) params(ctx context.Context, env map[string]any) (map[string]any, error) {
	refs, _ := env["input_artifacts"].([]any)
	if len(refs) == 0 {
		return map[string]any{}, nil
	}
	ref, _ := refs[0].(map[string]any)
	hash, _ := ref["sha256"].(string)
	raw, err := w.A.ReadArtifactFile(ctx, w.L, hash, "params.json")
	if err != nil {
		return nil, err
	}
	var params map[string]any
	if err := json.Unmarshal(raw, &params); err != nil {
		return nil, err
	}
	return params, nil
}

func (w *Worker) command(ctx context.Context, kind string, params map[string]any, outPath, jobDir string) (*exec.Cmd, error) {
	py := abs(filepath.Join(w.Root, "..", ".venv", "bin", "python"))
	bin := abs(filepath.Join(w.Root, "..", "bin"))
	switch kind {
	case "capture":
		mode := str(params["mode"], "live")
		args := []string{"--mode", mode, "--symbol", str(params["symbol"], "BTCUSDT"), "--root", str(params["raw_root"], "data/raw"), "--out", outPath}
		if mode == "archive" {
			args = append(args, "--date", str(params["date"], ""))
		} else {
			args = append(args, "--seconds", str(params["seconds"], "60"))
		}
		return exec.CommandContext(ctx, filepath.Join(bin, "qel-collect"), args...), nil
	case "prepare":
		return exec.CommandContext(ctx, py, "-m", "qel.cli", "prepare",
			"--events", str(params["events"], ""),
			"--manifest", str(params["manifest"], ""),
			"--out", str(params["out"], "data/normalized"),
			"--dataset-id", str(params["dataset_id"], "")), nil
	case "native-input":
		return exec.CommandContext(ctx, py, "-m", "qel.cli", "native-input",
			"--parquet", str(params["parquet"], ""),
			"--out", str(params["out"], "")), nil
	case "replay":
		return exec.CommandContext(ctx, filepath.Join(bin, "qel-native-runner"),
			"--events", str(params["events"], ""),
			"--strategy", str(params["strategy"], ""),
			"--out", outPath,
			"--notional", str(params["notional"], "100")), nil
	case "conformance":
		return exec.CommandContext(ctx, py, "-m", "qel.cli", "fixtures", "--out", outPath), nil
	case "audit":
		return exec.CommandContext(ctx, py, "-m", "qel.cli", "accounting", "--tape", str(params["tape"], "")), nil
	default:
		return nil, fmt.Errorf("unsupported job kind %s", kind)
	}
}

func (w *Worker) seal(ctx context.Context, job *queue.Job, report []byte, usage map[string]any) (map[string]any, error) {
	usageRaw, _ := json.Marshal(usage)
	files := []artifacts.FileInput{{Path: "report.json", Data: report}, {Path: "usage.json", Data: usageRaw}}
	indexHash := artifacts.IndexHash(files)
	manifest := map[string]any{
		"schema_version": "1.1.0",
		"artifact": map[string]any{
			"sha256": indexHash, "schema_id": "urn:qel:job:report", "schema_version": "1.1.0",
			"size_bytes": strconv.Itoa(len(report) + len(usageRaw)), "visibility": "DEVELOPMENT", "media_type": "application/json",
		},
		"producer_binary_hash": strings.Repeat("0", 64),
		"input_hashes":         []any{},
		"license_id":           "PROPRIETARY",
		"redistribution":       false,
		"created_at_ns":        strconv.FormatInt(time.Now().UnixNano(), 10),
		"complete":             true,
		"parent_hashes":        []any{},
		"row_count":            "1",
		"semantic_hash":        indexHash,
	}
	if _, err := w.A.Seal(ctx, w.L, manifest, files, nil); err != nil {
		return nil, err
	}
	return manifest["artifact"].(map[string]any), nil
}

func (w *Worker) fail(ctx context.Context, job *queue.Job, code string, cause error, started time.Time) {
	usage := map[string]any{
		"attempt_id": job.AttemptID, "wall_ns": strconv.FormatInt(time.Since(started).Nanoseconds(), 10),
		"cpu_ns": "0", "cgroup_peak_bytes": "0", "rss_peak_bytes": "0", "pss_peak_bytes": nil,
		"read_bytes": "0", "written_bytes": "0",
	}
	h, _ := hashAny(usage)
	result := map[string]any{
		"job_id": job.JobID, "attempt_id": job.AttemptID, "run_key": strings.Repeat("0", 64),
		"fencing_generation": float64(job.Generation), "status": "FAILED", "reason_code": code,
		"outputs": []any{}, "semantic_output_hash": nil, "usage_hash": h, "source": "backtest",
	}
	// run_key must be a digest; recover it from the envelope.
	var env map[string]any
	_ = json.Unmarshal(job.Envelope, &env)
	if rk, ok := env["run_key"].(string); ok {
		result["run_key"] = rk
	}
	_, _ = w.Q.Result(ctx, result)
	_, _ = w.L.Append(ctx, ledger.Input{
		EventType:  "RunAborted",
		Payload:    map[string]any{"job_id": job.JobID, "reason": code, "error": cause.Error()},
		ReasonCode: &code,
	})
}

type limitedWriter struct {
	w *os.File
	n int64
}

func (l *limitedWriter) Write(p []byte) (int, error) {
	if l.n <= 0 {
		return len(p), nil
	}
	if int64(len(p)) > l.n {
		p = p[:l.n]
	}
	l.n -= int64(len(p))
	_, err := l.w.Write(p)
	return len(p), err
}

func hashAny(v any) (string, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	return artifacts.IndexHash([]artifacts.FileInput{{Path: "x", Data: raw}}), nil
}

func value(m map[string]any, key string) any {
	if v, ok := m[key]; ok {
		return v
	}
	return nil
}

func str(v any, fallback string) string {
	if s, ok := v.(string); ok && s != "" {
		return s
	}
	return fallback
}

func abs(p string) string {
	q, err := filepath.Abs(p)
	if err != nil {
		return p
	}
	return q
}
