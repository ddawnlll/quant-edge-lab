package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/ddawnlll/quant-edge-lab/labd/artifacts"
	"github.com/ddawnlll/quant-edge-lab/labd/ledger"
	"github.com/ddawnlll/quant-edge-lab/labd/queue"
	"github.com/ddawnlll/quant-edge-lab/labd/worker"
)

func main() {
	root := flag.String("root", "runtime", "runtime root")
	flag.Parse()
	if err := os.MkdirAll(*root, 0o750); err != nil {
		log.Fatal(err)
	}
	l, err := ledger.Open(filepath.Join(*root, "labd", "research.sqlite"))
	if err != nil {
		log.Fatal(err)
	}
	defer l.Close()
	a, err := artifacts.New(filepath.Join(*root, "artifacts"))
	if err != nil {
		log.Fatal(err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	w := worker.New(*root, queue.New(l), a, l)
	log.Printf("qel-worker starting root=%s", *root)
	if err := w.Run(ctx, fmt.Sprintf("qel-worker-%d", os.Getpid())); err != nil {
		log.Fatal(err)
	}
}
