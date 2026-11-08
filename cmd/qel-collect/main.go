package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/ddawnlll/quant-edge-lab/labd/collect"
)

func main() {
	mode := flag.String("mode", "live", "live or archive")
	root := flag.String("root", "data/raw", "raw data root")
	symbol := flag.String("symbol", "BTCUSDT", "instrument symbol")
	seconds := flag.Int("seconds", 60, "live capture seconds")
	date := flag.String("date", "", "archive UTC date (YYYY-MM-DD)")
	out := flag.String("out", "", "write report JSON to this path")
	flag.Parse()

	c, err := collect.New(*root, *symbol)
	if err != nil {
		log.Fatal(err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	var report map[string]any
	switch *mode {
	case "live":
		report, err = c.Capture(ctx, *seconds, time.Second)
	case "archive":
		if *date == "" {
			log.Fatal("--date is required for archive mode")
		}
		report, err = c.DownloadArchive(ctx, *date)
	default:
		log.Fatalf("unknown mode %s", *mode)
	}
	if err != nil {
		log.Fatal(err)
	}
	raw, _ := json.MarshalIndent(report, "", "  ")
	if *out != "" {
		if err := os.WriteFile(*out, append(raw, '\n'), 0o640); err != nil {
			log.Fatal(err)
		}
	} else {
		fmt.Println(string(raw))
	}
}
