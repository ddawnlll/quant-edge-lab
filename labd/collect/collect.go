// Package collect captures public Binance USD-M market data with provenance.
package collect

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/ddawnlll/quant-edge-lab/contracts/go/qelcontract"
)

const sourceID = "BINANCE_USDM_PUBLIC"

type Collector struct {
	Client  *http.Client
	Root    string
	Symbol  string
	Binary  string
	BaseURL string
}

func New(root, symbol string) (*Collector, error) {
	exe, _ := os.Executable()
	raw, _ := os.ReadFile(exe)
	return &Collector{
		Client:  &http.Client{Timeout: 15 * time.Second},
		Root:    root,
		Symbol:  symbol,
		Binary:  hash(raw),
		BaseURL: "https://fapi.binance.com",
	}, nil
}

func hash(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func (c *Collector) getJSON(ctx context.Context, path string, out any) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "qel-lab/0.1 public-data-capture")
	resp, err := c.Client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		return body, fmt.Errorf("HTTP %d: %s", resp.StatusCode, truncate(string(body), 200))
	}
	if out != nil {
		if err := json.Unmarshal(body, out); err != nil {
			return body, err
		}
	}
	return body, nil
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

type gap struct {
	GapID       string  `json:"gap_id"`
	StartNS     string  `json:"start_ns"`
	EndNS       *string `json:"end_ns"`
	LastGoodSeq *string `json:"last_good_seq"`
	NextSeenSeq *string `json:"next_seen_seq"`
	Reason      string  `json:"reason"`
}

// Capture polls best bid/ask and aggregate trades, writing raw frames.
func (c *Collector) Capture(ctx context.Context, seconds int, interval time.Duration) (map[string]any, error) {
	if seconds <= 0 {
		seconds = 60
	}
	captureID := "CAP-" + strings.ToUpper(strings.ReplaceAll(c.Symbol, "-", "_")) + "-" + strconv.FormatInt(time.Now().Unix(), 10)
	dir := filepath.Join(c.Root, sourceID, c.Symbol, time.Now().UTC().Format("2006-01-02"))
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, err
	}
	eventsPath := filepath.Join(dir, captureID+"-events.jsonl")
	f, err := os.OpenFile(eventsPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o640)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	w := bufio.NewWriter(f)
	defer w.Flush()

	var offset int64
	var gaps []gap
	var lastTick time.Time
	var lastTradeID int64
	start := time.Now()
	deadline := start.Add(time.Duration(seconds) * time.Second)
	var quoteCount, tradeCount, duplicates int
	tradeIDs := map[int64]bool{}

	write := func(recordType string, payload any, raw []byte, sourceTS int64) error {
		rec := map[string]any{
			"record_type":  recordType,
			"ts_recv_ns":   strconv.FormatInt(time.Now().UnixNano(), 10),
			"source_ts_ns": strconv.FormatInt(sourceTS*1_000_000, 10),
			"source_id":    sourceID,
			"symbol":       c.Symbol,
			"endpoint":     c.BaseURL,
			"raw_sha256":   hash(raw),
			"raw_offset":   strconv.FormatInt(offset, 10),
			"payload":      payload,
		}
		line, err := json.Marshal(rec)
		if err != nil {
			return err
		}
		if _, err := w.Write(line); err != nil {
			return err
		}
		if err := w.WriteByte('\n'); err != nil {
			return err
		}
		offset += int64(len(line)) + 1
		return nil
	}

	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		default:
		}
		var book struct {
			BidPrice string `json:"bidPrice"`
			BidQty   string `json:"bidQty"`
			AskPrice string `json:"askPrice"`
			AskQty   string `json:"askQty"`
			Time     int64  `json:"time"`
			UpdateID int64  `json:"lastUpdateId"`
		}
		body, err := c.getJSON(ctx, "/fapi/v1/ticker/bookTicker?symbol="+c.Symbol, &book)
		if err != nil {
			gaps = append(gaps, gap{GapID: "GAP-" + strconv.Itoa(len(gaps)+1), StartNS: strconv.FormatInt(time.Now().UnixNano(), 10), EndNS: nil, Reason: "DISCONNECT"})
			time.Sleep(interval)
			continue
		}
		if !lastTick.IsZero() && time.Since(lastTick) > 3*interval {
			g := strconv.FormatInt(lastTick.UnixNano(), 10)
			gaps = append(gaps, gap{GapID: "GAP-" + strconv.Itoa(len(gaps)+1), StartNS: g, EndNS: strptr(strconv.FormatInt(time.Now().UnixNano(), 10)), LastGoodSeq: nil, NextSeenSeq: nil, Reason: "CLOCK_JUMP"})
		}
		lastTick = time.Now()
		if err := write("Quote", book, body, book.Time); err != nil {
			return nil, err
		}
		quoteCount++

		var trades []map[string]any
		tradeBody, err := c.getJSON(ctx, "/fapi/v1/aggTrades?symbol="+c.Symbol+"&limit=1000", &trades)
		if err == nil && len(trades) > 0 {
			for _, t := range trades {
				id := jsonInt(t["a"])
				if id > 0 && tradeIDs[id] {
					duplicates++
					continue
				}
				tradeIDs[id] = true
				if id > lastTradeID {
					lastTradeID = id
				}
				ts := jsonInt(t["T"])
				if err := write("Trade", t, tradeBody, ts); err != nil {
					return nil, err
				}
				tradeCount++
			}
		}
		time.Sleep(interval)
	}

	// Funding and mark reference at capture time.
	var funding map[string]any
	if _, err := c.getJSON(ctx, "/fapi/v1/premiumIndex?symbol="+c.Symbol, &funding); err == nil {
		body, _ := json.Marshal(funding)
		_ = write("Mark", funding, body, jsonInt(funding["time"]))
	}
	var rates []map[string]any
	if _, err := c.getJSON(ctx, "/fapi/v1/fundingRate?symbol="+c.Symbol+"&limit=10", &rates); err == nil {
		body, _ := json.Marshal(rates)
		for _, r := range rates {
			_ = write("Funding", r, body, jsonInt(r["fundingTime"]))
		}
	}
	if err := w.Flush(); err != nil {
		return nil, err
	}
	if err := f.Sync(); err != nil {
		return nil, err
	}
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(eventsPath)
	if err != nil {
		return nil, err
	}
	end := time.Now()
	manifest := map[string]any{
		"capture_id":               captureID,
		"source_id":                sourceID,
		"endpoint":                 c.BaseURL + "/fapi/v1/ticker/bookTicker,/fapi/v1/aggTrades,/fapi/v1/premiumIndex,/fapi/v1/fundingRate",
		"endpoint_definition_hash": mustHash(map[string]any{"symbol": c.Symbol, "venue": "BINANCE_USDM", "endpoints": []any{"bookTicker", "aggTrades", "premiumIndex", "fundingRate"}}),
		"sessions":                 []any{map[string]any{"session_id": "SES-1", "connection_id": "CON-1", "receive_point": "TARGET_HOST", "clock_uncertainty_ns": "5000000", "start_ns": strconv.FormatInt(start.UnixNano(), 10), "end_ns": strconv.FormatInt(end.UnixNano(), 10)}},
		"segments":                 []any{map[string]any{"sha256": hash(raw), "schema_id": "urn:qel:raw:1:frame", "schema_version": "1.1.0", "size_bytes": strconv.FormatInt(info.Size(), 10), "visibility": "DEVELOPMENT", "media_type": "application/x-ndjson"}},
		"raw_format":               "RAW_FRAME_V1",
		"source_checksums":         []any{},
		"loss_events":              gapValues(gaps),
		"license_id":               "BINANCE_PUBLIC_DATA_TERMS",
		"redistribution":           false,
		"collector_binary_hash":    c.Binary,
	}
	if err := qelcontract.ValidateDef("RawCaptureManifest", manifest); err != nil {
		return nil, err
	}
	mraw, _ := json.MarshalIndent(manifest, "", "  ")
	if err := os.WriteFile(filepath.Join(dir, captureID+"-manifest.json"), mraw, 0o640); err != nil {
		return nil, err
	}
	return map[string]any{
		"capture_id":     captureID,
		"events_path":    eventsPath,
		"manifest_path":  filepath.Join(dir, captureID+"-manifest.json"),
		"event_sha256":   hash(raw),
		"quote_records":  quoteCount,
		"trade_records":  tradeCount,
		"duplicates":     duplicates,
		"gaps":           len(gaps),
		"bytes":          info.Size(),
		"start_ns":       strconv.FormatInt(start.UnixNano(), 10),
		"end_ns":         strconv.FormatInt(end.UnixNano(), 10),
		"manifest_valid": true,
	}, nil
}

func gapValues(gaps []gap) []any {
	out := make([]any, 0, len(gaps))
	for _, g := range gaps {
		raw, _ := json.Marshal(g)
		var m map[string]any
		_ = json.Unmarshal(raw, &m)
		out = append(out, m)
	}
	return out
}

func strptr(s string) *string { return &s }

func mustHash(v any) string { h, _ := qelcontract.HashJCS(v); return h }

func jsonInt(v any) int64 {
	switch x := v.(type) {
	case float64:
		return int64(x)
	case json.Number:
		n, _ := x.Int64()
		return n
	case int64:
		return x
	case string:
		n, _ := strconv.ParseInt(x, 10, 64)
		return n
	}
	return 0
}

// DownloadArchive fetches one daily Binance Vision archive and verifies its checksum.
func (c *Collector) DownloadArchive(ctx context.Context, date string) (map[string]any, error) {
	base := "https://data.binance.vision/data/futures/um/daily"
	files := []string{
		fmt.Sprintf("aggTrades/%s/%s-aggTrades-%s.zip", c.Symbol, c.Symbol, date),
		fmt.Sprintf("markPriceKlines/%s/1m/%s-1m-%s.zip", c.Symbol, c.Symbol, date),
	}
	dir := filepath.Join(c.Root, "ARCHIVE_BINANCE_USDM", c.Symbol, date)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, err
	}
	var segments []any
	for _, rel := range files {
		url := base + "/" + rel
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		resp, err := c.Client.Do(req)
		if err != nil {
			return nil, err
		}
		body, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			return nil, err
		}
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("%s: HTTP %d", rel, resp.StatusCode)
		}
		sumResp, err := c.Client.Get(url + ".CHECKSUM")
		if err != nil {
			return nil, err
		}
		sumBody, _ := io.ReadAll(sumResp.Body)
		sumResp.Body.Close()
		if sumResp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("%s: checksum HTTP %d", rel, sumResp.StatusCode)
		}
		want := strings.Fields(string(sumBody))[0]
		if hash(body) != want {
			return nil, fmt.Errorf("%s: SHA256 mismatch", rel)
		}
		name := filepath.Base(rel)
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, body, 0o640); err != nil {
			return nil, err
		}
		segments = append(segments, map[string]any{
			"sha256": hash(body), "schema_id": "urn:qel:raw:1:archive", "schema_version": "1.1.0",
			"size_bytes": strconv.Itoa(len(body)), "visibility": "DEVELOPMENT", "media_type": "application/zip",
		})
	}
	manifest := map[string]any{
		"capture_id":               "CAP-ARCHIVE-" + strings.ToUpper(strings.ReplaceAll(c.Symbol, "-", "_")) + "-" + strings.ReplaceAll(date, "-", ""),
		"source_id":                sourceID,
		"endpoint":                 base,
		"endpoint_definition_hash": mustHash(map[string]any{"venue": "BINANCE_USDM", "daily": date}),
		"sessions":                 []any{map[string]any{"session_id": "SES-ARCHIVE", "connection_id": "CON-ARCHIVE", "receive_point": "VENDOR_HOST", "clock_uncertainty_ns": nil, "start_ns": "0", "end_ns": "0"}},
		"segments":                 segments,
		"raw_format":               "SOURCE_ARCHIVE_BYTES",
		"source_checksums":         []any{},
		"loss_events":              []any{},
		"license_id":               "BINANCE_PUBLIC_DATA_TERMS",
		"redistribution":           false,
		"collector_binary_hash":    c.Binary,
	}
	if err := qelcontract.ValidateDef("RawCaptureManifest", manifest); err != nil {
		return nil, err
	}
	mraw, _ := json.MarshalIndent(manifest, "", "  ")
	mpath := filepath.Join(dir, "manifest.json")
	if err := os.WriteFile(mpath, mraw, 0o640); err != nil {
		return nil, err
	}
	return map[string]any{"manifest_path": mpath, "segments": len(segments), "date": date}, nil
}
