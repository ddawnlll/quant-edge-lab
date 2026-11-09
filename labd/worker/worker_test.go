package worker

import (
	"context"
	"path/filepath"
	"testing"
)

func TestSafePathContainment(t *testing.T) {
	root := t.TempDir()
	w := &Worker{Root: filepath.Join(root, "runtime")}
	inside, err := w.safePath("data/native/events.jsonl")
	if err != nil || inside != filepath.Join(root, "data", "native", "events.jsonl") {
		t.Fatalf("inside path: %q %v", inside, err)
	}
	for _, bad := range []string{"", "../outside", "../../etc/passwd", "/etc/passwd"} {
		if _, err := w.safePath(bad); err == nil {
			t.Fatalf("accepted outside path %q", bad)
		}
	}
}

func TestCommandRejectsTraversal(t *testing.T) {
	w := &Worker{Root: filepath.Join(t.TempDir(), "runtime")}
	_, err := w.command(context.Background(), "replay", map[string]any{
		"events":   "../../etc/passwd",
		"strategy": "specification/examples/strategy_mr.json",
	}, filepath.Join(w.Root, "jobs", "JOB-1", "report.json"))
	if err == nil {
		t.Fatal("traversal path accepted")
	}
}
