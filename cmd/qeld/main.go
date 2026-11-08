// qeld is the QEL supervisor: ledger, queue, control socket and dashboard.
package main

import (
	"context"
	crand "crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/ddawnlll/quant-edge-lab/labd/artifacts"
	"github.com/ddawnlll/quant-edge-lab/labd/control"
	"github.com/ddawnlll/quant-edge-lab/labd/ledger"
	"github.com/ddawnlll/quant-edge-lab/labd/queue"
	"github.com/ddawnlll/quant-edge-lab/labd/worker"
)

func main() {
	root := flag.String("root", ".", "runtime root")
	httpAddr := flag.String("http", "127.0.0.1:8787", "localhost dashboard address")
	socket := flag.String("socket", "", "control Unix socket (default <root>/qel.sock)")
	runWorker := flag.Bool("worker", false, "run an in-process job worker (separate qel-worker is preferred)")
	tokenPath := flag.String("http-token-file", "", "HTTP write token file (default <root>/http.token)")
	flag.Parse()

	if err := os.MkdirAll(*root, 0o750); err != nil {
		log.Fatal(err)
	}
	_ = os.Chmod(*root, 0o750)
	dbPath := filepath.Join(*root, "labd", "research.sqlite")
	l, err := ledger.Open(dbPath)
	if err != nil {
		log.Fatal(err)
	}
	defer l.Close()

	version, source, err := l.SQLiteVersion(context.Background())
	if err != nil {
		log.Fatal(err)
	}
	_ = os.Chmod(dbPath, 0o640)
	log.Printf("sqlite version=%s source_id=%s", version, source)
	if err := l.VerifyChain(context.Background()); err != nil {
		log.Fatalf("ledger chain: %v", err)
	}
	a, err := artifacts.New(filepath.Join(*root, "artifacts"))
	if err != nil {
		log.Fatal(err)
	}
	if *socket == "" {
		*socket = filepath.Join(*root, "qel.sock")
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	q := queue.New(l)
	if *tokenPath == "" {
		*tokenPath = filepath.Join(*root, "http.token")
	}
	token, err := os.ReadFile(*tokenPath)
	if err != nil || len(strings.TrimSpace(string(token))) < 32 {
		buf := make([]byte, 24)
		if _, err := crand.Read(buf); err != nil {
			log.Fatal(err)
		}
		text := hex.EncodeToString(buf)
		if err := os.WriteFile(*tokenPath, []byte(text+"\n"), 0o600); err != nil {
			log.Fatal(err)
		}
		token = []byte(text)
	}
	tokenText := strings.TrimSpace(string(token))
	s := control.New(l, q, a, *socket, *httpAddr)
	s.HTTPToken = tokenText
	if *runWorker {
		w := worker.New(*root, q, a, l)
		go func() {
			if err := w.Run(ctx, fmt.Sprintf("qeld-%d", os.Getpid())); err != nil && ctx.Err() == nil {
				log.Printf("worker stopped: %v", err)
			}
		}()
	}

	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if n, err := s.Q.RequeueExpired(ctx); err != nil {
					log.Printf("requeue: %v", err)
				} else if n > 0 {
					log.Printf("requeued %d expired leases", n)
				}
			}
		}
	}()

	pidPath := filepath.Join(*root, "qeld.pid")
	_ = os.WriteFile(pidPath, []byte(strconv.Itoa(os.Getpid())+"\n"), 0o640)
	log.Printf("qeld listening socket=%s http=%s db=%s", *socket, *httpAddr, dbPath)
	if err := s.Serve(ctx); err != nil {
		log.Fatal(err)
	}
	if err := l.Checkpoint(context.Background()); err != nil {
		log.Printf("checkpoint: %v", err)
	}
	_ = os.Remove(pidPath)
	fmt.Println("qeld stopped")
}
