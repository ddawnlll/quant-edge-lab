// qel-submit seals job parameters and submits a JobEnvelope to qeld.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/ddawnlll/quant-edge-lab/contracts/go/qelcontract"
)

func hash(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

func main() {
	_ = flag.String("root", "runtime", "runtime root")
	httpAddr := flag.String("http", "http://127.0.0.1:8787", "qeld HTTP base")
	kind := flag.String("kind", "conformance", "job kind")
	paramsPath := flag.String("params", "", "job params JSON")
	jobID := flag.String("job-id", "", "job id")
	wait := flag.Bool("wait", false, "poll until terminal")
	flag.Parse()
	if *paramsPath == "" {
		fmt.Fprintln(os.Stderr, "--params is required")
		os.Exit(2)
	}
	raw, err := os.ReadFile(*paramsPath)
	if err != nil {
		panic(err)
	}
	var params map[string]any
	if err := json.Unmarshal(raw, &params); err != nil {
		panic(err)
	}
	_ = params
	sealResp, err := http.Post(*httpAddr+"/v1/dev/params", "application/json", bytes.NewReader(raw))
	if err != nil {
		panic(err)
	}
	sealBody, _ := io.ReadAll(sealResp.Body)
	sealResp.Body.Close()
	if sealResp.StatusCode >= 300 {
		panic(string(sealBody))
	}
	var sealed struct {
		Artifact map[string]any `json:"artifact"`
	}
	if err := json.Unmarshal(sealBody, &sealed); err != nil {
		panic(err)
	}
	ref := sealed.Artifact
	if *jobID == "" {
		*jobID = "JOB-" + strconv.FormatInt(time.Now().UnixNano(), 10)
	}
	env := map[string]any{
		"schema_version":        "1.1.0",
		"job_id":                *jobID,
		"kind":                  *kind,
		"program_id":            "PROG-LAB",
		"family_id":             nil,
		"hypothesis_id":         nil,
		"trial_id":              nil,
		"run_key":               hash(append(raw, []byte(*kind)...)),
		"attempt_id":            "ATT-1",
		"input_artifacts":       []any{ref},
		"strategy_hash":         strings.Repeat("0", 64),
		"binary_hash":           strings.Repeat("0", 64),
		"environment_hash":      strings.Repeat("0", 64),
		"required_capabilities": []any{"CAP-TAKER-REPLAY"},
		"resource_limits": map[string]any{
			"memory_max_mib": 2048, "memory_high_mib": 1536, "cpu_millicores": 1500,
			"pids_max": 64, "threads_max": 2, "fds_max": 256, "scratch_max_mib": 512,
			"output_max_mib": 64, "stdout_max_mib": 16, "wall_seconds": 300,
			"spill_max_mib": 512, "swap_max_mib": 0,
		},
		"deadline_ns": "18446744073709551615",
		"seed_protocol": map[string]any{
			"algorithm": "HMAC_SHA256_COUNTER_V1", "root_seed_hex": strings.Repeat("0", 64),
			"labels": []any{"LATENCY"}, "replication_ids": []any{0},
			"draw_mapping": "U64_BE_REJECTION_MODULO", "latency_tape_hash": nil,
		},
		"output_schema_id": "urn:qel:job:report",
		"source":           "backtest",
		"idempotency_key":  strings.Repeat("0", 64),
		"lease_generation": 0,
		"financial_trial":  false,
	}
	env["idempotency_key"] = func() string {
		m := map[string]any{}
		for k, v := range env {
			if k != "idempotency_key" {
				m[k] = v
			}
		}
		h, _ := qelcontract.HashJCS(m)
		return h
	}()
	if err := qelcontract.ValidateDef("JobEnvelope", env); err != nil {
		panic(err)
	}
	body, _ := json.Marshal(env)
	resp, err := http.Post(*httpAddr+"/v1/jobs", "application/json", bytes.NewReader(body))
	if err != nil {
		panic(err)
	}
	defer resp.Body.Close()
	out, _ := io.ReadAll(resp.Body)
	fmt.Println(strings.TrimSpace(string(out)))
	if resp.StatusCode >= 300 {
		os.Exit(1)
	}
	if *wait {
		for i := 0; i < 600; i++ {
			time.Sleep(time.Second)
			r, err := http.Get(*httpAddr + "/v1/jobs/" + *jobID)
			if err != nil {
				continue
			}
			data, _ := io.ReadAll(r.Body)
			r.Body.Close()
			var status map[string]any
			_ = json.Unmarshal(data, &status)
			state, _ := status["state"].(string)
			if state == "DECIDED" || state == "FAILED" || state == "ABORTED" || state == "RESOURCE_LIMIT" {
				fmt.Println(strings.TrimSpace(string(data)))
				return
			}
		}
		os.Exit(3)
	}
}
