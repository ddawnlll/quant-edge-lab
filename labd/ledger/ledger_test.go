package ledger

import (
	"context"
	"database/sql"
	"errors"
	"path/filepath"
	"testing"
)

func openTest(t *testing.T) *Ledger {
	t.Helper()
	l, err := Open(filepath.Join(t.TempDir(), "research.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { l.Close() })
	return l
}

func TestAppendPageAndChain(t *testing.T) {
	l := openTest(t)
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		if _, err := l.Append(ctx, Input{EventType: "JobStateChanged", Payload: map[string]any{"n": i}}); err != nil {
			t.Fatal(err)
		}
	}
	if err := l.VerifyChain(ctx); err != nil {
		t.Fatal(err)
	}
	events, next, more, err := l.Page(ctx, "", 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 2 || !more || next != events[1].LedgerSeq {
		t.Fatalf("unexpected page: len=%d more=%v next=%s", len(events), more, next)
	}
	if events[0].PreviousHash != "0" {
		t.Fatalf("first previous hash = %s", events[0].PreviousHash)
	}
	if events[1].PreviousHash != events[0].EventHash {
		t.Fatalf("chain not linked")
	}
}

func TestIdempotency(t *testing.T) {
	l := openTest(t)
	ctx := context.Background()
	raw1, reused1, err := l.Idempotent(ctx, "KEY-2", "BODY-A", func(tx *sql.Tx) (any, error) { return map[string]any{"state": "OK"}, nil })
	if err != nil || reused1 || len(raw1) == 0 {
		t.Fatalf("first call: %v %v %s", err, reused1, raw1)
	}
	raw2, reused2, err := l.Idempotent(ctx, "KEY-2", "BODY-A", func(tx *sql.Tx) (any, error) { return map[string]any{"state": "WRONG"}, nil })
	if err != nil || !reused2 || string(raw2) != string(raw1) {
		t.Fatalf("duplicate call: %v %v %s", err, reused2, raw2)
	}
	_, _, err = l.Idempotent(ctx, "KEY-2", "BODY-B", func(tx *sql.Tx) (any, error) { return nil, nil })
	var conflict *ConflictError
	if !errors.As(err, &conflict) {
		t.Fatalf("expected conflict, got %v", err)
	}
}

func TestTamperedLedgerIsDetected(t *testing.T) {
	l := openTest(t)
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		if _, err := l.Append(ctx, Input{EventType: "JobStateChanged", Payload: map[string]any{"n": i}}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := l.db.ExecContext(ctx, `UPDATE ledger_events SET payload_hash=? WHERE seq='2'`, string(make([]byte, 64))); err != nil {
		t.Fatal(err)
	}
	if err := l.VerifyChain(ctx); err == nil {
		t.Fatal("tampered payload hash was not detected")
	}
}

func TestDeletedLedgerEventIsDetected(t *testing.T) {
	l := openTest(t)
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		if _, err := l.Append(ctx, Input{EventType: "JobStateChanged", Payload: map[string]any{"n": i}}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := l.db.ExecContext(ctx, `DELETE FROM ledger_events WHERE seq='2'`); err != nil {
		t.Fatal(err)
	}
	if err := l.VerifyChain(ctx); err == nil {
		t.Fatal("deleted middle event was not detected")
	}
}

func TestDeletedFirstLedgerEventIsDetected(t *testing.T) {
	l := openTest(t)
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		if _, err := l.Append(ctx, Input{EventType: "JobStateChanged", Payload: map[string]any{"n": i}}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := l.db.ExecContext(ctx, `DELETE FROM ledger_events WHERE seq='1'`); err != nil {
		t.Fatal(err)
	}
	if err := l.VerifyChain(ctx); err == nil {
		t.Fatal("deleted first event was not detected")
	}
}

func TestTamperedPayloadContentIsDetected(t *testing.T) {
	l := openTest(t)
	ctx := context.Background()
	if _, err := l.Append(ctx, Input{EventType: "JobStateChanged", Payload: map[string]any{"n": 1}}); err != nil {
		t.Fatal(err)
	}
	if _, err := l.db.ExecContext(ctx, `UPDATE ledger_events SET payload_json='{"n":99}' WHERE seq='1'`); err != nil {
		t.Fatal(err)
	}
	if err := l.VerifyChain(ctx); err == nil {
		t.Fatal("tampered payload content was not detected")
	}
}

func TestTamperedEventIDIsDetected(t *testing.T) {
	l := openTest(t)
	ctx := context.Background()
	if _, err := l.Append(ctx, Input{EventType: "JobStateChanged", Payload: map[string]any{"n": 1}}); err != nil {
		t.Fatal(err)
	}
	if _, err := l.db.ExecContext(ctx, `UPDATE ledger_events SET event_id=? WHERE seq='1'`, string(make([]byte, 64))); err != nil {
		t.Fatal(err)
	}
	if err := l.VerifyChain(ctx); err == nil {
		t.Fatal("tampered event id was not detected")
	}
}
