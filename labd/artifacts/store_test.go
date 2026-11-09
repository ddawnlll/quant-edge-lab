package artifacts

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"testing"

	"github.com/ddawnlll/quant-edge-lab/labd/ledger"
)

func manifestFor(files []FileInput) map[string]any {
	hash := IndexHash(files)
	return map[string]any{
		"schema_version":       "1.1.0",
		"producer_binary_hash": fmt.Sprintf("%064d", 0),
		"input_hashes":         []any{},
		"license_id":           "PROPRIETARY",
		"redistribution":       false,
		"created_at_ns":        "1",
		"complete":             true,
		"parent_hashes":        []any{},
		"row_count":            strconv.Itoa(len(files)),
		"semantic_hash":        hash,
		"artifact": map[string]any{
			"sha256":         hash,
			"schema_id":      "urn:qel:artifact:test",
			"schema_version": "1.1.0",
			"size_bytes":     strconv.Itoa(len(files)),
			"visibility":     "DEVELOPMENT",
			"media_type":     "application/json",
		},
	}
}

func setup(t *testing.T) (*Store, *ledger.Ledger) {
	t.Helper()
	dir := t.TempDir()
	l, err := ledger.Open(filepath.Join(dir, "research.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { l.Close() })
	s, err := New(filepath.Join(dir, "artifacts"))
	if err != nil {
		t.Fatal(err)
	}
	return s, l
}

func TestSealAndRead(t *testing.T) {
	s, l := setup(t)
	ctx := context.Background()
	files := []FileInput{{Path: "a.txt", Data: []byte("alpha")}, {Path: "b.txt", Data: []byte("beta")}}
	receipt, err := s.Seal(ctx, l, manifestFor(files), files, nil)
	if err != nil {
		t.Fatal(err)
	}
	if receipt["manifest_hash"] != IndexHash(files) {
		t.Fatalf("wrong hash %v", receipt)
	}
	blob, err := s.ReadFile(fmt.Sprintf("%064x", 0))
	_ = blob
	if err == nil {
		t.Fatal("missing blob unexpectedly readable")
	}
	if err := l.VerifyChain(ctx); err != nil {
		t.Fatal(err)
	}
	report, err := s.Scan(ctx, l)
	if err != nil {
		t.Fatal(err)
	}
	if report.OrphanFinals != 0 || report.AckedCommitLost != 0 || report.PartialPackages != 0 {
		t.Fatalf("unexpected recovery report %+v", report)
	}
}

func TestFaultMatrixKeepsInvariants(t *testing.T) {
	s, l := setup(t)
	ctx := context.Background()
	points := []string{"WRITE_TEMP", "VALIDATE", "FILE_FSYNC", "RENAME", "DIR_FSYNC", "BEFORE_SQL_COMMIT", "AFTER_SQL_COMMIT"}
	for i, point := range points {
		files := []FileInput{{Path: "a.txt", Data: []byte(fmt.Sprintf("payload-%d-%s", i, point))}}
		manifest := manifestFor(files)
		_, err := s.Seal(ctx, l, manifest, files, func(p string) error {
			if p == point {
				return errors.New("injected crash")
			}
			return nil
		})
		if point == "AFTER_SQL_COMMIT" {
			// The commit was durable before the injected failure.
			if err == nil {
				t.Fatal("expected injected error")
			}
		} else if err == nil {
			t.Fatalf("%s did not inject", point)
		}
	}
	report, err := s.Scan(ctx, l)
	if err != nil {
		t.Fatal(err)
	}
	if report.PartialPackages != 0 {
		t.Fatalf("partial packages visible: %d", report.PartialPackages)
	}
	if report.AckedCommitLost != 0 {
		t.Fatalf("acked commit lost: %d", report.AckedCommitLost)
	}
	if !report.OrphanFinalFound {
		t.Fatalf("orphan final not detected: %+v", report)
	}
	if !report.FailureRetained {
		t.Fatalf("failure usage not retained: %+v", report)
	}
}

func TestTamperedBlobIsRejected(t *testing.T) {
	s, l := setup(t)
	ctx := context.Background()
	files := []FileInput{{Path: "a.txt", Data: []byte("alpha")}}
	if _, err := s.Seal(ctx, l, manifestFor(files), files, nil); err != nil {
		t.Fatal(err)
	}
	hash := hashBytes(files[0].Data)
	if _, err := s.ReadFile(hash); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(s.blobPath(hash), []byte("tampered"), 0o640); err != nil {
		t.Fatal(err)
	}
	if _, err := s.ReadFile(hash); err == nil {
		t.Fatal("tampered blob was accepted")
	}
}

func TestResealRepairsCorruptBlob(t *testing.T) {
	s, l := setup(t)
	ctx := context.Background()
	files := []FileInput{{Path: "a.txt", Data: []byte("alpha")}}
	manifest := manifestFor(files)
	if _, err := s.Seal(ctx, l, manifest, files, nil); err != nil {
		t.Fatal(err)
	}
	hash := hashBytes(files[0].Data)
	if err := os.WriteFile(s.blobPath(hash), []byte("tampered"), 0o640); err != nil {
		t.Fatal(err)
	}
	receipt, err := s.Seal(ctx, l, manifest, files, nil)
	if err != nil {
		t.Fatal(err)
	}
	if receipt["reused"] != true {
		t.Fatalf("expected idempotent receipt, got %v", receipt)
	}
	raw, err := s.ReadFile(hash)
	if err != nil || string(raw) != "alpha" {
		t.Fatalf("blob not repaired: %q %v", raw, err)
	}
	if err := l.VerifyChain(ctx); err != nil {
		t.Fatal(err)
	}
}
