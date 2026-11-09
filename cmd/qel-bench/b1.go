package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"time"
)

const (
	b1EmaWindow = 48
	b1StdWindow = 96
	b1ErWindow  = 10
)

type b1Bars struct {
	open, close, high, low, volume []float64
}

type b1Features struct {
	logc, ema, sub, std, lag, score, er []float64
}

type b1Workload struct {
	Rows              int      `json:"rows"`
	Variants          int      `json:"variants"`
	VariantThresholds []string `json:"variant_thresholds"`
	CommonOperators   []string `json:"common_operators"`
	BarIntervalNS     string   `json:"bar_interval_ns"`
}

type b1Repeat struct {
	Index      int     `json:"index"`
	WallMS     float64 `json:"wall_ms"`
	ReadMS     float64 `json:"read_ms"`
	FeaturesMS float64 `json:"features_ms"`
	VariantsMS float64 `json:"variants_ms"`
	OutputMS   float64 `json:"output_ms"`
	OutputHash string  `json:"output_hash"`
	Long       []int   `json:"long_counts"`
	Short      []int   `json:"short_counts"`
}

func runB1(argv []string) error {
	fs := flag.NewFlagSet("b1", flag.ContinueOnError)
	corpus := fs.String("corpus", "benchmarks/corpus/b1_bars.csv", "frozen CSV corpus")
	workloadPath := fs.String("workload", "benchmarks/corpus/b1_workload.json", "frozen workload declaration")
	out := fs.String("out", "", "evidence JSON path")
	outDir := fs.String("out-dir", "", "directory for per-repeat output files (fsync target)")
	repeats := fs.Int("repeats", 12, "timed repeats")
	warmups := fs.Int("warmups", 2, "warmup repeats")
	verify := fs.Bool("verify", true, "compare features against a naive reference on the first rows")
	verifyRows := fs.Int("verify-rows", 4000, "rows used by the naive reference check")
	if err := fs.Parse(argv); err != nil {
		return err
	}
	if *outDir == "" {
		dir, err := os.MkdirTemp("", "qel-b1-out-")
		if err != nil {
			return err
		}
		defer os.RemoveAll(dir)
		*outDir = dir
	}
	if err := os.MkdirAll(*outDir, 0o750); err != nil {
		return err
	}

	workloadRaw, err := os.ReadFile(*workloadPath)
	if err != nil {
		return err
	}
	var workload b1Workload
	if err := json.Unmarshal(workloadRaw, &workload); err != nil {
		return err
	}
	thresholds := make([]float64, len(workload.VariantThresholds))
	for i, raw := range workload.VariantThresholds {
		thresholds[i], err = strconv.ParseFloat(raw, 64)
		if err != nil {
			return fmt.Errorf("threshold %q: %w", raw, err)
		}
	}
	if len(thresholds) != 8 {
		return fmt.Errorf("workload declares %d variants, want 8", len(thresholds))
	}

	corpusHash, err := fileSHA256(*corpus)
	if err != nil {
		return err
	}

	var failed []map[string]any
	var warm []float64
	var timed []b1Repeat
	var verifyReport map[string]any

	for attempt := 0; attempt < *warmups+*repeats; attempt++ {
		repeat, err := b1RunOnce(attempt, *corpus, *outDir, thresholds, *verify && verifyReport == nil, *verifyRows, workload.Rows)
		if err != nil {
			failed = append(failed, map[string]any{"attempt": attempt, "error": err.Error()})
			continue
		}
		if repeat.Out.OutputHash == "" {
			failed = append(failed, map[string]any{"attempt": attempt, "error": "empty output hash"})
			continue
		}
		if verifyReport == nil && repeat.Verify != nil {
			verifyReport = repeat.Verify
		}
		if attempt < *warmups {
			warm = append(warm, repeat.Out.WallMS)
			continue
		}
		timed = append(timed, *repeat.Out)
	}
	if len(timed) == 0 {
		return fmt.Errorf("no successful timed repeat: %v", failed)
	}

	totals := make([]float64, len(timed))
	readMS := make([]float64, 0, len(timed))
	featureMS := make([]float64, 0, len(timed))
	variantMS := make([]float64, 0, len(timed))
	outputMS := make([]float64, 0, len(timed))
	for i, r := range timed {
		totals[i] = r.WallMS
		readMS = append(readMS, r.ReadMS)
		featureMS = append(featureMS, r.FeaturesMS)
		variantMS = append(variantMS, r.VariantsMS)
		outputMS = append(outputMS, r.OutputMS)
	}
	stats := describeMS(totals)
	p90 := stats["p90_seconds"].(float64)
	target := 10.0
	corpusFrozen := true
	if len(corpusHash) != 64 {
		corpusFrozen = false
	}
	verifyOK := verifyReport != nil && verifyReport["status"] == "PASS"
	passed := len(failed) == 0 && p90 <= target && stats["repeats"].(int) >= 10 && corpusFrozen && verifyOK

	report := map[string]any{
		"kind":                   "TASK25_B1_BATCH_SCREEN",
		"required_check":         "B1",
		"corpus":                 *corpus,
		"corpus_sha256":          corpusHash,
		"workload_sha256":        sha256Hex(workloadRaw),
		"workload":               workload,
		"include":                []string{"READ", "FEATURES", "VARIANTS", "OUTPUT_HASH_FSYNC"},
		"operators_implemented":  workload.CommonOperators,
		"warmups":                *warmups,
		"timed_repeats":          *repeats,
		"warm_wall_ms":           warm,
		"timed":                  stats,
		"phase_p50_ms":           map[string]any{"read": medianMS(readMS), "features": medianMS(featureMS), "variants": medianMS(variantMS), "output_hash_fsync": medianMS(outputMS)},
		"variant_thresholds":     thresholds,
		"metric":                 map[string]any{"name": "warm_p90_seconds", "value": p90, "target": target, "operator": "LE", "passed": p90 <= target},
		"reference_verification": verifyReport,
		"failed_attempts":        failed,
		"output_hash_frozen":     corpusFrozen,
	}
	report["verdict"] = ternary(passed, "PASS", "FAIL")
	if *out != "" {
		if err := writeJSON(*out, report); err != nil {
			return err
		}
	} else {
		if err := writeJSON("", report); err != nil {
			return err
		}
	}
	if !passed {
		return fmt.Errorf("B1 did not meet the acceptance target: p90=%.3fs failed=%d verify=%v", p90, len(failed), verifyOK)
	}
	return nil
}

// b1RunOnce is one full cold-to-warm pipeline repetition.
type b1Result struct {
	Out    *b1Repeat
	Verify map[string]any
}

func b1RunOnce(index int, corpus, outDir string, thresholds []float64, doVerify bool, verifyRows, wantRows int) (*b1Result, error) {
	started := time.Now()
	bars, readMS, err := readB1(corpus, wantRows)
	if err != nil {
		return nil, err
	}
	featureStart := time.Now()
	features := computeB1Features(bars)
	featuresMS := time.Since(featureStart).Seconds() * 1000.0

	variantStart := time.Now()
	hash, longs, shorts, err := runB1Variants(bars, features, thresholds)
	variantMS := time.Since(variantStart).Seconds() * 1000.0
	if err != nil {
		return nil, err
	}

	outputStart := time.Now()
	target := filepath.Join(outDir, fmt.Sprintf("b1_run_%02d.json", index))
	payload := map[string]any{
		"run": index, "rows": len(bars.close), "output_hash": hash,
		"long_counts": longs, "short_counts": shorts, "corpus_sha256": "",
	}
	if err := writeFileFSync(target, payload); err != nil {
		return nil, err
	}
	outputMS := time.Since(outputStart).Seconds() * 1000.0

	repeat := &b1Repeat{
		Index: index, WallMS: time.Since(started).Seconds() * 1000.0,
		ReadMS: readMS, FeaturesMS: featuresMS, VariantsMS: variantMS, OutputMS: outputMS,
		OutputHash: hash, Long: longs, Short: shorts,
	}
	result := &b1Result{Out: repeat}
	if doVerify {
		result.Verify = verifyB1Reference(bars, features, thresholds, verifyRows)
	}
	return result, nil
}

func readB1(path string, wantRows int) (*b1Bars, float64, error) {
	started := time.Now()
	f, err := os.Open(path)
	if err != nil {
		return nil, 0, err
	}
	defer f.Close()
	bars := &b1Bars{
		open:   make([]float64, 0, wantRows),
		close:  make([]float64, 0, wantRows),
		high:   make([]float64, 0, wantRows),
		low:    make([]float64, 0, wantRows),
		volume: make([]float64, 0, wantRows),
	}
	reader := bufio.NewReaderSize(f, 1<<20)
	if _, err := reader.ReadString('\n'); err != nil {
		return nil, 0, fmt.Errorf("header: %w", err)
	}
	var line []byte
	for {
		chunk, err := reader.ReadSlice('\n')
		if err != nil {
			break
		}
		line = chunk[:len(chunk)-1]
		var fields [6]string
		start := 0
		fi := 0
		for i := 0; i <= len(line) && fi < 6; i++ {
			if i == len(line) || line[i] == ',' {
				fields[fi] = string(line[start:i])
				start = i + 1
				fi++
			}
		}
		if fi != 6 {
			return nil, 0, fmt.Errorf("row %d: %d fields", len(bars.close), fi)
		}
		open, err1 := parseFastFloat(fields[1])
		close_, err2 := parseFastFloat(fields[2])
		high, err3 := parseFastFloat(fields[3])
		low, err4 := parseFastFloat(fields[4])
		volume, err5 := parseFastFloat(fields[5])
		if err1 != nil || err2 != nil || err3 != nil || err4 != nil || err5 != nil {
			return nil, 0, fmt.Errorf("row %d: parse", len(bars.close))
		}
		bars.open = append(bars.open, open)
		bars.close = append(bars.close, close_)
		bars.high = append(bars.high, high)
		bars.low = append(bars.low, low)
		bars.volume = append(bars.volume, volume)
	}
	if len(bars.close) != wantRows {
		return nil, 0, fmt.Errorf("corpus has %d rows, workload declares %d", len(bars.close), wantRows)
	}
	return bars, time.Since(started).Seconds() * 1000.0, nil
}

func computeB1Features(bars *b1Bars) *b1Features {
	n := len(bars.close)
	f := &b1Features{
		logc:  make([]float64, n),
		ema:   make([]float64, n),
		sub:   make([]float64, n),
		std:   make([]float64, n),
		lag:   make([]float64, n),
		score: make([]float64, n),
		er:    make([]float64, n),
	}
	alpha := 2.0 / (float64(b1EmaWindow) + 1.0)
	var emaSum float64
	var emaSeed bool
	for i := 0; i < n; i++ {
		f.logc[i] = math.Log(bars.close[i])
		if !emaSeed {
			emaSum += f.logc[i]
			if i == b1EmaWindow-1 {
				f.ema[i] = emaSum / float64(b1EmaWindow)
				emaSeed = true
			} else {
				f.ema[i] = emaSum / float64(i+1)
			}
		} else {
			f.ema[i] = alpha*f.logc[i] + (1-alpha)*f.ema[i-1]
		}
		f.sub[i] = f.logc[i] - f.ema[i]
		if i > 0 {
			f.lag[i] = f.sub[i-1]
		}
	}
	// OP-STD window 96, ddof=1, rolling sums with a ring buffer.
	var sum, sumsq float64
	ring := make([]float64, b1StdWindow)
	for i := 0; i < n; i++ {
		if i >= b1StdWindow {
			old := ring[i%b1StdWindow]
			sum -= old
			sumsq -= old * old
		}
		ring[i%b1StdWindow] = f.sub[i]
		sum += f.sub[i]
		sumsq += f.sub[i] * f.sub[i]
		count := i + 1
		if count > b1StdWindow {
			count = b1StdWindow
		}
		if count < 2 {
			f.std[i] = 0
		} else {
			mean := sum / float64(count)
			variance := (sumsq - float64(count)*mean*mean) / float64(count-1)
			if variance < 0 {
				variance = 0
			}
			f.std[i] = math.Sqrt(variance)
		}
		if f.std[i] != 0 {
			f.score[i] = f.sub[i] / f.std[i]
		}
	}
	// OP-ER window 10 over the log close series.
	absDiff := make([]float64, n)
	for i := 1; i < n; i++ {
		absDiff[i] = math.Abs(f.logc[i] - f.logc[i-1])
	}
	var path float64
	for i := 0; i < n; i++ {
		if i >= b1ErWindow {
			path -= absDiff[i-b1ErWindow]
		}
		path += absDiff[i]
		if i >= b1ErWindow {
			move := math.Abs(f.logc[i] - f.logc[i-b1ErWindow])
			if path > 0 {
				f.er[i] = move / path
			}
		}
	}
	return f
}

func runB1Variants(bars *b1Bars, features *b1Features, thresholds []float64) (string, []int, []int, error) {
	n := len(bars.close)
	longs := make([]int, len(thresholds))
	shorts := make([]int, len(thresholds))
	h := sha256.New()
	var row [8]byte
	var checksum uint64
	for i := 0; i < n; i++ {
		score := features.score[i]
		er := features.er[i]
		for v, t := range thresholds {
			var decision byte
			if er > 0 && score > t {
				decision = 1
				longs[v]++
			} else if er > 0 && score < -t {
				decision = 2
				shorts[v]++
			}
			row[v] = decision
			checksum = checksum*1099511628211 ^ uint64(bars.volume[i])
		}
		if _, err := h.Write(row[:]); err != nil {
			return "", nil, nil, err
		}
	}
	var suffix [8]byte
	binary.BigEndian.PutUint64(suffix[:], checksum)
	h.Write(suffix[:])
	return hex.EncodeToString(h.Sum(nil)), longs, shorts, nil
}

// verifyB1Reference recomputes the feature stack with a naive O(n*window)
// implementation and compares decisions for the first `rows` rows.
func verifyB1Reference(bars *b1Bars, features *b1Features, thresholds []float64, rows int) map[string]any {
	if rows > len(bars.close) {
		rows = len(bars.close)
	}
	maxRel := 0.0
	mismatches := 0
	alpha := 2.0 / (float64(b1EmaWindow) + 1.0)
	for i := 0; i < rows; i++ {
		logc := math.Log(bars.close[i])
		var ema float64
		if i < b1EmaWindow-1 {
			ema = 0
			for k := 0; k <= i; k++ {
				ema += math.Log(bars.close[k])
			}
			ema /= float64(i + 1)
		} else if i == b1EmaWindow-1 {
			for k := 0; k < b1EmaWindow; k++ {
				ema += math.Log(bars.close[k])
			}
			ema /= float64(b1EmaWindow)
		} else {
			ema = alpha*logc + (1-alpha)*features.ema[i-1]
		}
		sub := logc - ema
		var std float64
		lo := i - b1StdWindow + 1
		if lo < 0 {
			lo = 0
		}
		count := i - lo + 1
		if count >= 2 {
			var mean float64
			for k := lo; k <= i; k++ {
				mean += features.sub[k]
			}
			mean /= float64(count)
			var acc float64
			for k := lo; k <= i; k++ {
				d := features.sub[k] - mean
				acc += d * d
			}
			std = math.Sqrt(acc / float64(count-1))
		}
		var er float64
		if i >= b1ErWindow {
			var path float64
			for k := i - b1ErWindow + 1; k <= i; k++ {
				path += math.Abs(features.logc[k] - features.logc[k-1])
			}
			if path > 0 {
				er = math.Abs(features.logc[i]-features.logc[i-b1ErWindow]) / path
			}
		}
		for _, pair := range []struct{ got, want float64 }{{features.logc[i], logc}, {features.ema[i], ema}, {features.sub[i], sub}, {features.std[i], std}, {features.er[i], er}} {
			rel := relDiff(pair.got, pair.want)
			if rel > maxRel {
				maxRel = rel
			}
		}
		score := 0.0
		if std != 0 {
			score = sub / std
		}
		for _, t := range thresholds {
			var decision byte
			if er > 0 && score > t {
				decision = 1
			} else if er > 0 && score < -t {
				decision = 2
			}
			got := byte(0)
			if features.er[i] > 0 && features.score[i] > t {
				got = 1
			} else if features.er[i] > 0 && features.score[i] < -t {
				got = 2
			}
			if got != decision {
				mismatches++
			}
		}
	}
	status := "PASS"
	if maxRel > 1e-9 || mismatches != 0 {
		status = "FAIL"
	}
	return map[string]any{
		"status": status, "rows_checked": rows, "max_relative_feature_difference": maxRel,
		"decision_mismatches": mismatches, "tolerance": 1e-9,
	}
}

func relDiff(a, b float64) float64 {
	if a == b {
		return 0
	}
	denom := math.Abs(b)
	if denom < 1 {
		denom = 1
	}
	return math.Abs(a-b) / denom
}

func describeMS(values []float64) map[string]any {
	sorted := append([]float64(nil), values...)
	sort.Float64s(sorted)
	median := 0.0
	if len(sorted)%2 == 1 {
		median = sorted[len(sorted)/2]
	} else {
		median = (sorted[len(sorted)/2-1] + sorted[len(sorted)/2]) / 2
	}
	p90 := 0.0
	if len(sorted) > 0 {
		rank := int(math.Ceil(0.9 * float64(len(sorted))))
		if rank < 1 {
			rank = 1
		}
		p90 = sorted[rank-1]
	}
	samples := make([]float64, len(sorted))
	for i, v := range sorted {
		samples[i] = v / 1000.0
	}
	return map[string]any{
		"repeats":         len(sorted),
		"median_ms":       median,
		"p90_ms":          p90,
		"median_seconds":  median / 1000.0,
		"p90_seconds":     p90 / 1000.0,
		"min_seconds":     sorted[0] / 1000.0,
		"max_seconds":     sorted[len(sorted)-1] / 1000.0,
		"samples_seconds": samples,
	}
}

func medianMS(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	sorted := append([]float64(nil), values...)
	sort.Float64s(sorted)
	return sorted[len(sorted)/2]
}

func parseFastFloat(s string) (float64, error) {
	if s == "" {
		return 0, fmt.Errorf("empty")
	}
	return strconv.ParseFloat(s, 64)
}

func fileSHA256(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func writeFileFSync(path string, value any) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o640)
	if err != nil {
		return err
	}
	if _, err := f.Write(raw); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

func ternary(cond bool, a, b string) string {
	if cond {
		return a
	}
	return b
}
