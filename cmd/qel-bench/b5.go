package main

import (
	"bufio"
	"context"
	"database/sql"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/ddawnlll/quant-edge-lab/labd/artifacts"
	"github.com/ddawnlll/quant-edge-lab/labd/ledger"
)

type b5Event struct {
	EventType     string         `json:"event_type"`
	EffectiveAtNS string         `json:"effective_at_ns"`
	Payload       map[string]any `json:"payload"`
	RecordScope   string         `json:"record_scope"`
}

type b5Artifact struct {
	ArtifactID string `json:"artifact_id"`
	Path       string `json:"path"`
	Sha256     string `json:"sha256"`
	SizeBytes  int    `json:"size_bytes"`
}

type b5ArtifactManifest struct {
	LedgerEvents     int          `json:"ledger_events"`
	ArtifactPointers int          `json:"artifact_pointers"`
	Pointers         []b5Artifact `json:"artifact_pointers_list"`
}

func runB5(argv []string) error {
	fs := flag.NewFlagSet("b5", flag.ContinueOnError)
	eventsPath := fs.String("events", "benchmarks/corpus/b5_events.jsonl", "frozen ledger event corpus")
	artifactsPath := fs.String("artifacts", "benchmarks/corpus/b5_artifacts.json", "frozen artifact pointer declaration")
	workDir := fs.String("work-dir", "", "scratch directory (default: temp dir)")
	out := fs.String("out", "", "evidence JSON path")
	repeats := fs.Int("repeats", 12, "timed read-transaction repeats")
	warmups := fs.Int("warmups", 2, "read-transaction warmups")
	crashTrials := fs.Int("crash-trials", 6, "F17 crash-matrix trials")
	batchSize := fs.Int("batch", 500, "events per durable transaction")
	skipCrash := fs.Bool("skip-crash", false, "skip the F17 crash matrix")
	crashOnly := fs.Bool("crash-only", false, "run only the F17 crash matrix")
	if err := fs.Parse(argv); err != nil {
		return err
	}
	if *workDir == "" {
		dir, err := os.MkdirTemp("", "qel-b5-")
		if err != nil {
			return err
		}
		defer os.RemoveAll(dir)
		*workDir = dir
	}
	if err := os.MkdirAll(*workDir, 0o750); err != nil {
		return err
	}

	eventsRaw, err := os.ReadFile(*eventsPath)
	if err != nil {
		return err
	}
	artifactsRaw, err := os.ReadFile(*artifactsPath)
	if err != nil {
		return err
	}
	var declaration b5ArtifactManifest
	if err := json.Unmarshal(artifactsRaw, &declaration); err != nil {
		return err
	}
	events, err := parseB5Events(eventsRaw)
	if err != nil {
		return err
	}
	if len(events) != declaration.LedgerEvents {
		return fmt.Errorf("corpus has %d events, declaration says %d", len(events), declaration.LedgerEvents)
	}

	ctx := context.Background()
	if *crashOnly {
		crashReport, err := runCrashMatrix(ctx, *eventsPath, *workDir, *crashTrials, events)
		if err != nil {
			return fmt.Errorf("crash matrix: %w", err)
		}
		return writeJSON(*out, map[string]any{
			"kind": "TASK25_B5_CRASH_MATRIX", "required_check": "B5",
			"fixture": "F17", "events_corpus_sha256": sha256Hex(eventsRaw),
			"trials": *crashTrials, "report": crashReport,
			"verdict": crashReport["status"],
		})
	}
	ledgerPath := filepath.Join(*workDir, "research.sqlite")
	artifactRoot := filepath.Join(*workDir, "artifacts")
	l, err := ledger.Open(ledgerPath)
	if err != nil {
		return err
	}
	defer l.Close()
	store, err := artifacts.New(artifactRoot)
	if err != nil {
		return err
	}

	// Phase 1: seal the 1,000 artifact pointers. Each seal appends an
	// ArtifactSealed ledger event with the artifact descriptor as a ref.
	sealStart := time.Now()
	sealed := make([]b5Artifact, 0, len(declaration.Pointers))
	for _, pointer := range declaration.Pointers {
		data := b5ArtifactBytes(pointer.ArtifactID)
		files := []artifacts.FileInput{{Path: pointer.Path, Data: data}}
		indexHash := artifacts.IndexHash(files)
		manifest := map[string]any{
			"schema_version":       "1.1.0",
			"producer_binary_hash": strings.Repeat("0", 64),
			"input_hashes":         []any{},
			"license_id":           "PROPRIETARY",
			"redistribution":       false,
			"created_at_ns":        "1",
			"complete":             true,
			"parent_hashes":        []any{},
			"row_count":            "1",
			"semantic_hash":        indexHash,
			"artifact": map[string]any{
				"sha256":         indexHash,
				"schema_id":      "urn:qel:artifact:b5",
				"schema_version": "1.1.0",
				"size_bytes":     strconv.Itoa(len(data)),
				"visibility":     "DEVELOPMENT",
				"media_type":     "application/octet-stream",
			},
		}
		receipt, err := store.Seal(ctx, l, manifest, files, nil)
		if err != nil {
			return fmt.Errorf("seal %s: %w", pointer.ArtifactID, err)
		}
		if receipt["manifest_hash"] != indexHash {
			return fmt.Errorf("seal %s returned manifest hash %v, want %s", pointer.ArtifactID, receipt["manifest_hash"], indexHash)
		}
		sealed = append(sealed, pointer)
	}
	sealWallMS := time.Since(sealStart).Seconds() * 1000.0

	// Phase 2: append the 100,000-event corpus. Only the first 1,000
	// ArtifactPointer events carry an explicit artifact_ref; the declaration
	// already has 1,000 distinct pointers, so every pointer is represented.
	// Events are appended in durable batches. Each batch is one SQLite
	// transaction with synchronous=FULL, so a batch is either fully committed
	// and fsynced or fully rolled back. The hash chain spans batches.
	appendStart := time.Now()
	var pointerRefs int
	byType := map[string]int{}
	var commits int
	for _, ev := range events {
		byType[ev.EventType]++
	}
	commits, pointerRefs, err = appendEvents(ctx, l, events, declaration.Pointers, *batchSize, nil)
	if err != nil {
		return fmt.Errorf("append: %w", err)
	}
	appendWallMS := time.Since(appendStart).Seconds() * 1000.0

	if err := l.VerifyChain(ctx); err != nil {
		return fmt.Errorf("chain after append: %w", err)
	}
	var totalEvents int64
	if err := l.DB().QueryRowContext(ctx, `SELECT COUNT(*) FROM ledger_events`).Scan(&totalEvents); err != nil {
		return err
	}
	scan, err := store.Scan(ctx, l)
	if err != nil {
		return fmt.Errorf("artifact scan: %w", err)
	}

	// Phase 3: read transaction p90.
	var failed []map[string]any
	var warm []float64
	var timed []float64
	var readRows []int64
	for attempt := 0; attempt < *warmups+*repeats; attempt++ {
		ms, rows, err := readTransaction(ctx, l)
		if err != nil {
			failed = append(failed, map[string]any{"attempt": attempt, "phase": "READ_TRANSACTION", "error": err.Error()})
			continue
		}
		readRows = append(readRows, rows)
		if attempt < *warmups {
			warm = append(warm, ms)
			continue
		}
		timed = append(timed, ms)
	}
	if len(timed) == 0 {
		return fmt.Errorf("no successful read transaction")
	}
	readStats := describeMS(timed)
	readP90 := readStats["p90_ms"].(float64)

	// Phase 4: SQLite backup and metadata restore.
	backupPath := filepath.Join(*workDir, "backup", "research.sqlite")
	restoreStart := time.Now()
	checkpointErr := l.Checkpoint(ctx)
	backupErr := l.Backup(ctx, backupPath)
	var restore chainReport
	if backupErr == nil {
		restore, err = restoreAndVerify(ctx, backupPath, artifactRoot, l)
		if err != nil {
			backupErr = err
		}
	}
	restoreWall := time.Since(restoreStart).Seconds()
	restoreMinutes := restoreWall / 60.0
	if checkpointErr != nil && backupErr == nil {
		backupErr = checkpointErr
	}
	if backupErr != nil {
		return fmt.Errorf("backup/restore: %w", backupErr)
	}

	// Phase 5: F17 crash matrix.
	crashReport := map[string]any{"status": "SKIPPED"}
	if !*skipCrash {
		crashReport, err = runCrashMatrix(ctx, *eventsPath, *workDir, *crashTrials, events)
		if err != nil {
			return fmt.Errorf("crash matrix: %w", err)
		}
	}

	lost := crashReport["lost_committed_decisions"]
	torn := crashReport["torn_pass"]
	lostOK := lost == float64(0) || lost == 0
	tornOK := torn == float64(0) || torn == 0
	crashOK := *skipCrash || crashReport["status"] == "PASS"
	restoreOK := restore.chainValid && restore.eventCount == totalEvents && restore.scan.PartialPackages == 0 && restore.scan.AckedCommitLost == 0
	readOK := readP90 <= 2000
	restoreMinutesOK := restoreMinutes <= 60
	passed := lostOK && tornOK && readOK && restoreMinutesOK && restoreOK && crashOK && len(failed) == 0

	report := map[string]any{
		"kind":                         "TASK25_B5_LEDGER_RESTORE",
		"required_check":               "B5",
		"events_corpus":                *eventsPath,
		"events_corpus_sha256":         sha256Hex(eventsRaw),
		"artifact_declaration_sha256":  sha256Hex(artifactsRaw),
		"ledger_events_target":         declaration.LedgerEvents,
		"ledger_events_appended":       len(events),
		"ledger_events_total":          totalEvents,
		"artifact_pointers_sealed":     len(sealed),
		"artifact_pointers_referenced": pointerRefs,
		"event_type_counts":            byType,
		"seal_wall_ms":                 sealWallMS,
		"append_wall_ms":               appendWallMS,
		"append_batch_events":          *batchSize,
		"append_transactions":          commits,
		"append_events_per_second":     float64(len(events)) / (appendWallMS / 1000.0),
		"sqlite_synchronous":           "FULL",
		"journal_mode":                 "WAL",
		"read_transaction": map[string]any{
			"warmups": *warmups, "repeats": *repeats, "warm_ms": warm,
			"median_ms": readStats["median_ms"], "p90_ms": readP90,
			"min_ms": readStats["min_ms"], "max_ms": readStats["max_ms"],
			"samples_ms": readStats["samples_ms"], "rows_per_transaction": readRows,
		},
		"restore": map[string]any{
			"mechanism":    "SQLITE_VACUUM_INTO_BACKUP_THEN_OPEN_AND_FULL_HASH_CHAIN",
			"wall_seconds": restoreWall, "minutes": restoreMinutes,
			"chain_valid": restore.chainValid, "events": restore.eventCount,
			"artifact_scan": restore.scan,
		},
		"crash_matrix": map[string]any{
			"fixture": "F17", "trials": *crashTrials, "report": crashReport,
		},
		"artifact_scan_after_append": scan,
		"targets": map[string]any{
			"lost_committed_decisions": map[string]any{"value": lost, "target": 0, "operator": "EQ", "passed": lostOK},
			"torn_pass":                map[string]any{"value": torn, "target": 0, "operator": "EQ", "passed": tornOK},
			"read_transaction_ms":      map[string]any{"value": readP90, "target": 2000, "operator": "LE", "passed": readOK},
			"metadata_restore_minutes": map[string]any{"value": restoreMinutes, "target": 60, "operator": "LE", "passed": restoreMinutesOK},
		},
		"failed_attempts": failed,
		"verdict":         ternary(passed, "PASS", "FAIL"),
	}
	if err := writeJSON(*out, report); err != nil {
		return err
	}
	if !passed {
		return fmt.Errorf("B5 did not meet acceptance targets: lost=%v torn=%v read_p90=%.1fms restore=%.3fmin restoreOK=%v crash=%v", lost, torn, readP90, restoreMinutes, restoreOK, crashReport["status"])
	}
	return nil
}

type chainReport struct {
	chainValid bool
	eventCount int64
	scan       artifacts.RecoveryReport
}

func restoreAndVerify(ctx context.Context, backupPath, artifactRoot string, original *ledger.Ledger) (chainReport, error) {
	report := chainReport{}
	restored, err := ledger.Open(backupPath)
	if err != nil {
		return report, err
	}
	defer restored.Close()
	if err := restored.VerifyChain(ctx); err != nil {
		return report, err
	}
	report.chainValid = true
	if err := restored.DB().QueryRowContext(ctx, `SELECT COUNT(*) FROM ledger_events`).Scan(&report.eventCount); err != nil {
		return report, err
	}
	// Artifact manifests and blobs live outside SQLite. Re-scan the restored
	// database against the same store to prove metadata consistency.
	_ = original
	store, err := artifacts.New(artifactRoot)
	if err != nil {
		return report, err
	}
	scan, err := store.Scan(ctx, restored)
	if err != nil {
		return report, err
	}
	report.scan = scan
	return report, nil
}

// readTransaction reads the full ledger inside one read-only SQL transaction
// and re-checks the in-memory hash linkage it observes.
func readTransaction(ctx context.Context, l *ledger.Ledger) (float64, int64, error) {
	started := time.Now()
	tx, err := l.DB().BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return 0, 0, err
	}
	defer tx.Rollback()
	rows, err := tx.QueryContext(ctx, `SELECT seq, event_id, event_hash, previous_hash FROM ledger_events ORDER BY seq`)
	if err != nil {
		return 0, 0, err
	}
	defer rows.Close()
	var count int64
	previous := "0"
	for rows.Next() {
		var seq int64
		var eventID, hash, prev string
		if err := rows.Scan(&seq, &eventID, &hash, &prev); err != nil {
			return 0, 0, err
		}
		if seq != count+1 {
			return 0, 0, fmt.Errorf("seq gap at %d", seq)
		}
		if prev != previous {
			return 0, 0, fmt.Errorf("chain break at seq %d", seq)
		}
		if len(eventID) != 64 || len(hash) != 64 {
			return 0, 0, fmt.Errorf("bad hash length at seq %d", seq)
		}
		previous = hash
		count++
	}
	if err := rows.Err(); err != nil {
		return 0, 0, err
	}
	rows.Close()
	if err := tx.Commit(); err != nil {
		return 0, 0, err
	}
	return time.Since(started).Seconds() * 1000.0, count, nil
}

func parseB5Events(raw []byte) ([]b5Event, error) {
	scanner := bufio.NewScanner(strings.NewReader(string(raw)))
	scanner.Buffer(make([]byte, 1<<20), 1<<20)
	events := make([]b5Event, 0, 100_000)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var ev b5Event
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			return nil, fmt.Errorf("event %d: %w", len(events), err)
		}
		events = append(events, ev)
	}
	return events, scanner.Err()
}

func b5ArtifactBytes(id string) []byte {
	out := make([]byte, 64)
	copy(out, []byte(id))
	return out
}

// appendEvents writes events in durable batches. Each batch is one SQLite
// transaction with synchronous=FULL; onCommit (if any) runs after the commit
// returns, so an acknowledged batch is durable.
func appendEvents(ctx context.Context, l *ledger.Ledger, events []b5Event, pointers []b5Artifact, batch int, onCommit func([]ledger.Event)) (int, int, error) {
	if batch < 1 {
		batch = 1
	}
	commits := 0
	pointerRefs := 0
	for start := 0; start < len(events); start += batch {
		end := start + batch
		if end > len(events) {
			end = len(events)
		}
		tx, err := l.DB().BeginTx(ctx, nil)
		if err != nil {
			return commits, pointerRefs, err
		}
		committed := make([]ledger.Event, 0, end-start)
		failed := false
		for _, ev := range events[start:end] {
			refs := []any{}
			if ev.EventType == "ArtifactPointer" && pointerRefs < len(pointers) {
				p := pointers[pointerRefs]
				refs = append(refs, map[string]any{
					"artifact_id": p.ArtifactID,
					"sha256":      p.Sha256,
				})
				pointerRefs++
			}
			event, err := l.AppendTx(ctx, tx, ledger.Input{
				EventType:     ev.EventType,
				EffectiveAtNS: ev.EffectiveAtNS,
				Payload:       ev.Payload,
				ArtifactRefs:  refs,
				RecordScope:   ev.RecordScope,
			})
			if err != nil {
				failed = true
				break
			}
			committed = append(committed, event)
		}
		if failed {
			tx.Rollback()
			return commits, pointerRefs, fmt.Errorf("batch at %d", start)
		}
		if err := tx.Commit(); err != nil {
			return commits, pointerRefs, err
		}
		commits++
		if onCommit != nil {
			onCommit(committed)
		}
	}
	return commits, pointerRefs, nil
}

// runCrashMatrix runs F17: SIGKILL a committing writer, then verify durable
// acknowledged decisions and torn PASS records on reopen.
func runCrashMatrix(ctx context.Context, eventsPath, workDir string, trials int, events []b5Event) (map[string]any, error) {
	delays := []time.Duration{
		2000 * time.Millisecond, 2200 * time.Millisecond, 2400 * time.Millisecond,
		2600 * time.Millisecond, 2800 * time.Millisecond, 3000 * time.Millisecond,
	}
	// The child needs roughly 0.7 s to open/migrate the ledger before its first
	// commit; the first two points still exercise a pre-commit crash because the
	// corpus is read and the batch prepared before the commit returns.
	trialReports := make([]map[string]any, 0, trials)
	lostTotal := 0
	tornTotal := 0
	allPass := true
	for trial := 0; trial < trials; trial++ {
		dir := filepath.Join(workDir, fmt.Sprintf("crash-%02d", trial))
		if err := os.MkdirAll(dir, 0o750); err != nil {
			return nil, err
		}
		ledgerPath := filepath.Join(dir, "crash.sqlite")
		ackPath := filepath.Join(dir, "ack.txt")
		exe, err := os.Executable()
		if err != nil {
			return nil, err
		}
		limit := 600
		if limit > len(events) {
			limit = len(events)
		}
		cmd := exec.Command(exe, "b5-child",
			"--ledger", ledgerPath, "--ack", ackPath,
			"--events", eventsPath, "--limit", strconv.Itoa(limit))
		cmd.Stdout = nil
		cmd.Stderr = os.Stderr
		if err := cmd.Start(); err != nil {
			return nil, err
		}
		delay := delays[trial%len(delays)]
		time.Sleep(delay)
		_ = cmd.Process.Signal(syscall.SIGKILL)
		_ = cmd.Wait()

		acked := readAck(ackPath)
		l, err := ledger.Open(ledgerPath)
		if err != nil {
			trialReports = append(trialReports, map[string]any{"trial": trial, "delay_ms": delay.Milliseconds(), "status": "FAIL", "error": err.Error()})
			allPass = false
			continue
		}
		chainErr := l.VerifyChain(ctx)
		var present int64
		if chainErr == nil {
			if err := l.DB().QueryRowContext(ctx, `SELECT COUNT(*) FROM ledger_events`).Scan(&present); err != nil {
				chainErr = err
			}
		}
		missing := int64(0)
		if chainErr == nil {
			for _, ack := range acked {
				var exists int
				if err := l.DB().QueryRowContext(ctx, `SELECT COUNT(*) FROM ledger_events WHERE seq=? AND event_hash=?`, ack.seq, ack.hash).Scan(&exists); err != nil {
					chainErr = err
					break
				}
				if exists == 0 {
					missing++
				}
			}
		}
		torn := 0
		if chainErr == nil {
			rows, err := l.DB().QueryContext(ctx, `SELECT payload_json FROM ledger_events WHERE event_type='PassRecorded'`)
			if err != nil {
				chainErr = err
			} else {
				for rows.Next() {
					var raw string
					if err := rows.Scan(&raw); err != nil {
						chainErr = err
						break
					}
					var payload map[string]any
					if err := json.Unmarshal([]byte(raw), &payload); err != nil {
						torn++
						continue
					}
					complete, _ := payload["complete"].(bool)
					if _, ok := payload["pass_id"]; !ok || !complete {
						torn++
					}
				}
				rows.Close()
			}
		}
		_ = l.Close()
		status := "PASS"
		if chainErr != nil || missing != 0 || torn != 0 {
			status = "FAIL"
			allPass = false
		}
		lostTotal += int(missing)
		tornTotal += torn
		entry := map[string]any{
			"trial": trial, "delay_ms": delay.Milliseconds(), "status": status,
			"acked_events": len(acked), "events_on_disk": present,
			"lost_committed_decisions": missing, "torn_pass": torn,
			"kill_signal": "SIGKILL",
		}
		if chainErr != nil {
			entry["error"] = chainErr.Error()
		}
		trialReports = append(trialReports, entry)
	}
	return map[string]any{
		"status":                   ternary(allPass, "PASS", "FAIL"),
		"lost_committed_decisions": lostTotal,
		"torn_pass":                tornTotal,
		"trials":                   trialReports,
	}, nil
}

type ackRecord struct {
	seq  int64
	hash string
}

func readAck(path string) []ackRecord {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	records := make([]ackRecord, 0, 1024)
	for _, line := range strings.Split(string(raw), "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 {
			continue
		}
		seq, err := strconv.ParseInt(fields[0], 10, 64)
		if err != nil {
			continue
		}
		records = append(records, ackRecord{seq: seq, hash: fields[1]})
	}
	return records
}

func runB5Child(argv []string) error {
	fs := flag.NewFlagSet("b5-child", flag.ContinueOnError)
	ledgerPath := fs.String("ledger", "", "ledger path")
	ackPath := fs.String("ack", "", "acknowledgement file")
	eventsPath := fs.String("events", "", "event corpus")
	limit := fs.Int("limit", 600, "maximum events")
	batchSize := fs.Int("batch", 50, "events per durable transaction")
	if err := fs.Parse(argv); err != nil {
		return err
	}
	raw, err := os.ReadFile(*eventsPath)
	if err != nil {
		return err
	}
	events, err := parseB5Events(raw)
	if err != nil {
		return err
	}
	if *limit < len(events) {
		events = events[:*limit]
	}
	l, err := ledger.Open(*ledgerPath)
	if err != nil {
		return err
	}
	defer l.Close()
	ack, err := os.OpenFile(*ackPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o640)
	if err != nil {
		return err
	}
	defer ack.Close()
	ctx := context.Background()
	batch := *batchSize
	if batch < 1 {
		batch = 1
	}
	_, _, err = appendEvents(ctx, l, events, nil, batch, func(committed []ledger.Event) {
		for _, event := range committed {
			seq, _ := strconv.ParseInt(event.LedgerSeq, 10, 64)
			if _, werr := fmt.Fprintf(ack, "%d %s\n", seq, event.EventHash); werr != nil {
				err = werr
				return
			}
		}
		if serr := ack.Sync(); serr != nil {
			err = serr
		}
	})
	return err
}
