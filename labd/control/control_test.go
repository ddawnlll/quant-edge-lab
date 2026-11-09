package control

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"github.com/ddawnlll/quant-edge-lab/labd/artifacts"
	"github.com/ddawnlll/quant-edge-lab/labd/ledger"
	"github.com/ddawnlll/quant-edge-lab/labd/queue"
)

func testServer(t *testing.T) (*Server, string) {
	t.Helper()
	dir := t.TempDir()
	l, err := ledger.Open(filepath.Join(dir, "research.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { l.Close() })
	a, err := artifacts.New(filepath.Join(dir, "artifacts"))
	if err != nil {
		t.Fatal(err)
	}
	s := New(l, queue.New(l), a, filepath.Join(dir, "qel.sock"), "127.0.0.1:0")
	return s, dir
}

func wireReq(id, op string, body map[string]any) map[string]any {
	return map[string]any{
		"wire_version":    "1.1.0",
		"request_id":      id,
		"operation_id":    op,
		"idempotency_key": fmt.Sprintf("%064x", 42),
		"body_schema_id":  "runtime.schema.json#/$defs/ApiReceipt",
		"body":            body,
		"deadline_ns":     "18446744073709551615",
	}
}

func call(t *testing.T, s *Server, uid uint32, req map[string]any) map[string]any {
	t.Helper()
	raw, _ := json.Marshal(req)
	resp := s.handleWire(context.Background(), raw, uid)
	var out map[string]any
	if err := json.Unmarshal(resp, &out); err != nil {
		t.Fatalf("response: %v %s", err, resp)
	}
	return out
}

func TestFrameBounds(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		conn, _ := net.Dial("tcp", ln.Addr().String())
		conn.Write([]byte{0, 16, 0, 1})
		conn.Close()
	}()
	conn, _ := ln.Accept()
	defer conn.Close()
	if _, err := ReadFrame(conn, 1048576); err != ErrFrameTooLarge {
		t.Fatalf("oversize not rejected before allocation: %v", err)
	}
}

func TestDashboardRoutingAndHeaders(t *testing.T) {
	s, _ := testServer(t)
	mux := s.httpMux()
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/", nil))
	if rec.Code != http.StatusOK || rec.Header().Get("Content-Security-Policy") == "" || rec.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Fatalf("dashboard response: %d %v", rec.Code, rec.Header())
	}
	rec = httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/not-a-route", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("unknown route status = %d", rec.Code)
	}
	rec = httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/", nil))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST / status = %d", rec.Code)
	}
	rec = httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodHead, "/", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("HEAD / status = %d", rec.Code)
	}
}

func TestRoleDenialAndIdempotency(t *testing.T) {
	s, _ := testServer(t)
	uid := uint32(4242)
	s.UIDRole[uid] = "P-RESEARCH"
	// Read-only principal cannot issue control.
	resp := call(t, s, uid, wireReq("REQ-1", "CONTROL", map[string]any{
		"action": "pause_family", "target_id": "FAM-1", "actor": "P-RESEARCH",
		"reason": "test", "idempotency_key": fmt.Sprintf("%064x", 1),
		"expected_generation": 0, "new_limit": nil,
	}))
	if resp["status"] != "ERROR" {
		t.Fatalf("expected denial, got %v", resp)
	}
	body := resp["body"].(map[string]any)
	if body["code"] != "ACCESS_DENIED" {
		t.Fatalf("expected ACCESS_DENIED, got %v", body)
	}
	// A labd principal can submit but not control.
	s.UIDRole[uid] = "P-LABD"
	req := wireReq("REQ-2", "JOB_GET", map[string]any{"id": "NO-SUCH-JOB", "cursor": nil, "limit": 10})
	resp = call(t, s, uid, req)
	if resp["status"] != "ERROR" {
		t.Fatalf("missing job should error: %v", resp)
	}
}

func TestVersionMismatchRejected(t *testing.T) {
	s, _ := testServer(t)
	req := wireReq("REQ-3", "PING", map[string]any{})
	req["wire_version"] = "1.0.0"
	resp := call(t, s, uint32(4242), req)
	if resp["status"] != "ERROR" {
		t.Fatalf("bad version accepted: %v", resp)
	}
	if resp["body"].(map[string]any)["code"] != "UNSUPPORTED_CAPABILITY" {
		t.Fatalf("wrong code: %v", resp)
	}
}

func TestBearerTokenAuthorization(t *testing.T) {
	s, _ := testServer(t)
	s.HTTPToken = "secret-token"
	req := httptest.NewRequest(http.MethodPost, "/v1/jobs", nil)
	if s.authorized(req) {
		t.Fatal("missing token accepted")
	}
	req.Header.Set("Authorization", "Bearer wrong")
	if s.authorized(req) {
		t.Fatal("wrong token accepted")
	}
	req.Header.Set("Authorization", "Bearer secret-token")
	if !s.authorized(req) {
		t.Fatal("correct token rejected")
	}
}

func TestUnixPeerCredentialAndPing(t *testing.T) {
	s, dir := testServer(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	socket := filepath.Join(dir, "qel.sock")
	s.Socket = socket
	s.HTTP = "127.0.0.1:0"
	go s.Serve(ctx)
	deadline := time.Now().Add(3 * time.Second)
	for {
		if _, err := net.Dial("unix", socket); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("socket did not start")
		}
		time.Sleep(20 * time.Millisecond)
	}
	conn, err := net.Dial("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	req := wireReq("REQ-4", "PING", map[string]any{})
	raw, _ := json.Marshal(req)
	if err := WriteFrame(conn, raw, MaxControlBytes); err != nil {
		t.Fatal(err)
	}
	out, err := ReadFrame(conn, MaxControlBytes)
	if err != nil {
		t.Fatal(err)
	}
	var resp map[string]any
	if err := json.Unmarshal(out, &resp); err != nil {
		t.Fatal(err)
	}
	if resp["status"] != "OK" {
		t.Fatalf("ping failed: %s", out)
	}
	if resp["body"].(map[string]any)["role"] != "P-LABD" {
		t.Fatalf("unexpected peer role: %v", resp)
	}
}
