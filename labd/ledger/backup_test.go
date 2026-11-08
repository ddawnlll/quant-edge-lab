package ledger

import (
	"context"
	"path/filepath"
	"testing"
)

func TestBackupRestoreChain(t *testing.T) {
	l := openTest(t)
	ctx := context.Background()
	for i := 0; i < 5; i++ {
		if _, err := l.Append(ctx, Input{EventType: "JobStateChanged", Payload: map[string]any{"i": i}}); err != nil {
			t.Fatal(err)
		}
	}
	backup := filepath.Join(t.TempDir(), "backup.sqlite")
	if err := l.Backup(ctx, backup); err != nil {
		t.Fatal(err)
	}
	restored, err := Open(backup)
	if err != nil {
		t.Fatal(err)
	}
	defer restored.Close()
	if err := restored.VerifyChain(ctx); err != nil {
		t.Fatalf("restored chain: %v", err)
	}
	events, _, _, err := restored.Page(ctx, "", 200)
	if err != nil || len(events) != 5 {
		t.Fatalf("restored events=%d err=%v", len(events), err)
	}
	if _, err := l.Append(ctx, Input{EventType: "JobStateChanged", Payload: map[string]any{"i": 5}}); err != nil {
		t.Fatal(err)
	}
}
