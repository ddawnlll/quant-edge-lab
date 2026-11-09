package collect

import (
	"context"
	"testing"
)

func TestCollectorRejectsUnsafePathInputs(t *testing.T) {
	for _, symbol := range []string{"", "../BTCUSDT", "BTC/USDT", "btcusdt"} {
		if _, err := New(t.TempDir(), symbol); err == nil {
			t.Fatalf("accepted symbol %q", symbol)
		}
	}
	c, err := New(t.TempDir(), "BTCUSDT")
	if err != nil {
		t.Fatal(err)
	}
	for _, date := range []string{"", "../2026-11-07", "2026-13-01", "2026-1-1"} {
		if _, err := c.DownloadArchive(context.Background(), date); err == nil {
			t.Fatalf("accepted date %q", date)
		}
	}
}
