package memory

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/ddawnlll/quant-edge-lab/labd/ledger"
)

func TestClaimsAndCAS(t *testing.T) {
	l, err := ledger.Open(filepath.Join(t.TempDir(), "research.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	ctx := context.Background()
	m := New(l)
	valid := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	if err := m.AddClaim(ctx, "CLM-1", "volatility mean reversion survives costs", valid); err != nil {
		t.Fatal(err)
	}
	got, err := m.Search(ctx, "mean", 10)
	if err != nil || len(got) != 1 {
		t.Fatalf("search: %v %v", got, err)
	}
	if _, err := m.Rebuild(ctx); err != nil {
		t.Fatal(err)
	}
	if err := m.AddTask(ctx, "TASK-A", map[string]any{"kind": "research"}); err != nil {
		t.Fatal(err)
	}
	gen1, err := m.Assign(ctx, "TASK-A", "agent-1", 50*time.Millisecond)
	if err != nil || gen1 != 1 {
		t.Fatalf("assign: %v %v", gen1, err)
	}
	if _, err := m.Assign(ctx, "TASK-A", "agent-2", time.Minute); err == nil {
		t.Fatal("live assignment stolen")
	}
	time.Sleep(60 * time.Millisecond)
	gen2, err := m.Assign(ctx, "TASK-A", "agent-2", time.Minute)
	if err != nil || gen2 != 2 {
		t.Fatalf("stale assignment: %v %v", gen2, err)
	}
	if err := m.Complete(ctx, "TASK-A", "agent-1", gen1, "DONE"); err == nil {
		t.Fatal("stale completion accepted")
	}
	if err := m.Complete(ctx, "TASK-A", "agent-2", gen2, "DONE"); err != nil {
		t.Fatal(err)
	}
}
