package control

import (
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/ddawnlll/quant-edge-lab/contracts/go/qelcontract"
	"github.com/ddawnlll/quant-edge-lab/labd/artifacts"
	"github.com/ddawnlll/quant-edge-lab/labd/ledger"
	"github.com/ddawnlll/quant-edge-lab/labd/memory"
	"github.com/ddawnlll/quant-edge-lab/labd/queue"
)

const MaxControlBytes = 1048576

type ProtocolError struct {
	Code    string
	Message string
}

func (e *ProtocolError) Error() string { return e.Code + ": " + e.Message }

func perr(code, msg string) *ProtocolError { return &ProtocolError{Code: code, Message: msg} }

type Server struct {
	L         *ledger.Ledger
	Q         *queue.Queue
	A         *artifacts.Store
	M         *memory.Store
	Socket    string
	HTTP      string
	HTTPToken string
	UIDRole   map[uint32]string
	mu        sync.Mutex
}

func New(l *ledger.Ledger, q *queue.Queue, a *artifacts.Store, socket, httpAddr string) *Server {
	return &Server{L: l, Q: q, A: a, M: memory.New(l), Socket: socket, HTTP: httpAddr, UIDRole: map[uint32]string{uint32(os.Getuid()): "P-LABD"}}
}

func (s *Server) role(uid uint32) string {
	if r, ok := s.UIDRole[uid]; ok {
		return r
	}
	return "P-RESEARCH"
}

func (s *Server) authorized(r *http.Request) bool {
	if s.HTTPToken == "" {
		return false
	}
	return r.Header.Get("Authorization") == "Bearer "+s.HTTPToken
}

func allowedRole(role, op string) bool {
	switch op {
	case "PING":
		return true
	case "JOB_SUBMIT", "HYPOTHESIS_REGISTER":
		return role == "P-LABD" || role == "P-RESEARCH"
	case "JOB_GET", "LEDGER_PAGE", "DASHBOARD":
		return role == "P-LABD" || role == "P-RESEARCH" || role == "P-OPERATOR"
	case "CONTROL":
		return role == "P-OPERATOR"
	case "WORKER_RESULT":
		return role == "P-WORKER" || role == "P-AUDIT" || role == "P-COLLECT" || role == "P-EVALUATOR"
	case "ARTIFACT_SEAL":
		return role == "P-LABD"
	}
	return false
}

func (s *Server) handleWire(ctx context.Context, raw []byte, uid uint32) []byte {
	var req map[string]any
	if !utf8.Valid(raw) {
		return errorResponse("REQ-UNKNOWN", "PROTOCOL_VIOLATION", "invalid UTF-8")
	}
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		return errorResponse("REQ-UNKNOWN", "SCHEMA_INVALID", err.Error())
	}
	if err := qelcontract.ValidateDef("WireRequest", req); err != nil {
		return errorResponse("REQ-UNKNOWN", "UNSUPPORTED_CAPABILITY", err.Error())
	}
	requestID, _ := req["request_id"].(string)
	op, _ := req["operation_id"].(string)
	deadline, _ := req["deadline_ns"].(string)
	if dl, err := strconv.ParseUint(deadline, 10, 64); err != nil || uint64(time.Now().UnixNano()) > dl {
		return errorResponse(requestID, "PROTOCOL_VIOLATION", "deadline expired")
	}
	role := s.role(uid)
	if !allowedRole(role, op) {
		_ = s.deny(ctx, uid, role, op, requestID)
		return errorResponse(requestID, "ACCESS_DENIED", "principal not allowed")
	}
	body, _ := req["body"].(map[string]any)
	var result map[string]any
	var err error
	switch op {
	case "PING":
		result = map[string]any{"status": "OK", "role": role}
	case "JOB_SUBMIT":
		result, err = s.Q.Submit(ctx, body)
	case "JOB_GET":
		result, err = s.jobStatus(ctx, body)
	case "LEDGER_PAGE":
		result, err = s.ledgerPage(ctx, body)
	case "DASHBOARD":
		result, err = s.Dashboard(ctx)
	case "WORKER_RESULT":
		result, err = s.workerResult(ctx, body)
	case "HYPOTHESIS_REGISTER":
		result, err = s.hypothesis(ctx, body, role)
	case "CONTROL":
		result, err = s.control(ctx, body, role)
	case "ARTIFACT_SEAL":
		result, err = s.seal(ctx, body)
	default:
		return errorResponse(requestID, "UNSUPPORTED_CAPABILITY", "unknown operation")
	}
	if err != nil {
		code := "PROTOCOL_VIOLATION"
		var pe *ProtocolError
		if errors.As(err, &pe) {
			code = pe.Code
		}
		return errorResponse(requestID, code, err.Error())
	}
	resp := map[string]any{
		"wire_version":   "1.1.0",
		"request_id":     requestID,
		"status":         "OK",
		"body_schema_id": "runtime.schema.json#/$defs/ApiReceipt",
		"body":           result,
	}
	out, _ := json.Marshal(resp)
	return out
}

func (s *Server) deny(ctx context.Context, uid uint32, role, op, requestID string) error {
	_, err := s.L.Append(ctx, ledger.Input{
		EventType:   "AccessDenied",
		Payload:     map[string]any{"uid": strconv.FormatUint(uint64(uid), 10), "role": role, "operation": op, "request_id": requestID},
		ReasonCode:  strptr("ACCESS_DENIED"),
		RecordScope: "OPERATIONS",
	})
	return err
}

func strptr(v string) *string { return &v }

func errorResponse(requestID, code, message string) []byte {
	resp := map[string]any{
		"wire_version":   "1.1.0",
		"request_id":     requestID,
		"status":         "ERROR",
		"body_schema_id": "runtime.schema.json#/$defs/Error",
		"body": map[string]any{
			"code": code, "retryable": false, "request_id": requestID, "details_artifact": nil,
		},
	}
	out, _ := json.Marshal(resp)
	return out
}

func (s *Server) jobStatus(ctx context.Context, body map[string]any) (map[string]any, error) {
	if err := qelcontract.ValidateDef("ReadRequest", body); err != nil {
		return nil, perr("SCHEMA_INVALID", err.Error())
	}
	id, _ := body["id"].(string)
	return s.Q.Status(ctx, id)
}

func (s *Server) ledgerPage(ctx context.Context, body map[string]any) (map[string]any, error) {
	if err := qelcontract.ValidateDef("ReadRequest", body); err != nil {
		return nil, perr("SCHEMA_INVALID", err.Error())
	}
	cursor := ""
	if c, ok := body["cursor"].(string); ok {
		cursor = c
	}
	limit := 200
	if l, ok := body["limit"].(float64); ok {
		limit = int(l)
	}
	events, next, more, err := s.L.Page(ctx, cursor, limit)
	if err != nil {
		return nil, err
	}
	page := map[string]any{"events": events, "next_cursor": nil, "has_more": more}
	if next != "" {
		page["next_cursor"] = next
	}
	if err := qelcontract.ValidateDef("LedgerPage", page); err != nil {
		return nil, err
	}
	return page, nil
}

func (s *Server) hypothesis(ctx context.Context, body map[string]any, role string) (map[string]any, error) {
	if err := qelcontract.ValidateDef("HypothesisSpec", body); err != nil {
		return nil, perr("SCHEMA_INVALID", err.Error())
	}
	key, _ := body["hypothesis_id"].(string)
	raw, _, err := s.L.Idempotent(ctx, "HYP-"+key, mustHash(body), func(tx *sql.Tx) (any, error) {
		ev, err := s.L.AppendTx(ctx, tx, ledger.Input{
			EventType:      "HypothesisRegistered",
			HypothesisID:   &key,
			Payload:        body,
			RecordScope:    "EXPERIMENT",
			ActorPrincipal: role,
			Role:           role,
		})
		if err != nil {
			return nil, err
		}
		return map[string]any{
			"request_id": "REQ-HYP-" + key, "resource_id": key, "ledger_event_hash": ev.EventHash,
			"state": "REGISTERED", "generation": 0,
		}, nil
	})
	if err != nil {
		return nil, err
	}
	var receipt map[string]any
	return receipt, json.Unmarshal(raw, &receipt)
}

func mustHash(v any) string { h, _ := qelcontract.HashJCS(v); return h }

func (s *Server) workerResult(ctx context.Context, body map[string]any) (map[string]any, error) {
	committed, err := s.Q.Result(ctx, body)
	if err != nil {
		var pe *ProtocolError
		if errors.As(err, &pe) {
			return nil, err
		}
		return nil, perr("SCHEMA_INVALID", err.Error())
	}
	jobID, _ := body["job_id"].(string)
	state := "REJECTED_STALE"
	if committed {
		state = "COMMITTED"
	}
	gen := int64(0)
	if g, ok := body["fencing_generation"].(float64); ok {
		gen = int64(g)
	}
	return map[string]any{
		"request_id": "REQ-RESULT-" + jobID, "resource_id": jobID,
		"ledger_event_hash": strings.Repeat("0", 64), "state": state,
		"generation": gen,
	}, nil
}

func (s *Server) control(ctx context.Context, body map[string]any, role string) (map[string]any, error) {
	if err := qelcontract.ValidateDef("ControlCommand", body); err != nil {
		return nil, perr("SCHEMA_INVALID", err.Error())
	}
	target, _ := body["target_id"].(string)
	action, _ := body["action"].(string)
	expected := int64(0)
	if g, ok := body["expected_generation"].(float64); ok {
		expected = int64(g)
	}
	if action == "stop_job" {
		status, err := s.Q.Status(ctx, target)
		if err != nil {
			return nil, perr("SCHEMA_INVALID", err.Error())
		}
		gen, _ := status["generation"].(int64)
		if gen != expected {
			return nil, perr("STALE_GENERATION", "expected generation does not match")
		}
		if err := s.Q.Transition(ctx, target, "ABORT", expected); err != nil {
			return nil, perr("PROTOCOL_VIOLATION", err.Error())
		}
	}
	key, _ := body["idempotency_key"].(string)
	raw, _, err := s.L.Idempotent(ctx, "CTL-"+key, mustHash(body), func(tx *sql.Tx) (any, error) {
		ev, err := s.L.AppendTx(ctx, tx, ledger.Input{
			EventType:      "OperatorOverride",
			Payload:        body,
			ReasonCode:     strptr("ACCESS_DENIED"),
			RecordScope:    "OPERATIONS",
			ActorPrincipal: role,
			Role:           role,
		})
		if err != nil {
			return nil, err
		}
		return map[string]any{
			"request_id": "REQ-CTL-" + target, "resource_id": target, "ledger_event_hash": ev.EventHash,
			"state": "ISSUED", "generation": expected,
		}, nil
	})
	if err != nil {
		return nil, err
	}
	var receipt map[string]any
	return receipt, json.Unmarshal(raw, &receipt)
}

func (s *Server) seal(ctx context.Context, body map[string]any) (map[string]any, error) {
	manifest, _ := body["manifest"].(map[string]any)
	rawFiles, _ := body["files"].([]any)
	var files []artifacts.FileInput
	for _, rf := range rawFiles {
		m, ok := rf.(map[string]any)
		if !ok {
			continue
		}
		path, _ := m["path"].(string)
		data, _ := m["data_base64"].(string)
		decoded, err := base64.StdEncoding.DecodeString(data)
		if err != nil {
			return nil, perr("INVALID_DATA", "invalid base64 payload")
		}
		files = append(files, artifacts.FileInput{Path: path, Data: decoded})
	}
	result, err := s.A.Seal(ctx, s.L, manifest, files, nil)
	if err != nil {
		return nil, perr("ARTIFACT_INCOMPLETE", err.Error())
	}
	return map[string]any{
		"request_id": "REQ-SEAL", "resource_id": result["manifest_hash"],
		"ledger_event_hash": result["ledger_event_hash"], "state": "SEALED", "generation": 0,
	}, nil
}

// Serve starts the Unix socket and localhost HTTP facade.
func (s *Server) Serve(ctx context.Context) error {
	_ = os.Remove(s.Socket)
	if err := os.MkdirAll(filepath.Dir(s.Socket), 0o750); err != nil {
		return err
	}
	ln, err := net.Listen("unix", s.Socket)
	if err != nil {
		return err
	}
	if err := os.Chmod(s.Socket, 0o660); err != nil {
		ln.Close()
		return err
	}
	mux := s.httpMux()
	httpServer := &http.Server{Addr: s.HTTP, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(shutdownCtx)
		ln.Close()
	}()
	go func() {
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			fmt.Fprintln(os.Stderr, "http:", err)
		}
	}()
	for {
		conn, err := ln.Accept()
		if err != nil {
			select {
			case <-ctx.Done():
				return nil
			default:
				return err
			}
		}
		go s.handleConn(ctx, conn)
	}
}

func (s *Server) handleConn(ctx context.Context, conn net.Conn) {
	defer conn.Close()
	uid, err := PeerUID(conn)
	if err != nil {
		return
	}
	for {
		raw, err := ReadFrame(conn, MaxControlBytes)
		if err != nil {
			return
		}
		resp := s.handleWire(ctx, raw, uid)
		if err := WriteFrame(conn, resp, MaxControlBytes); err != nil {
			return
		}
	}
}

func (s *Server) httpMux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		version, source, err := s.L.SQLiteVersion(r.Context())
		if err != nil {
			writeJSON(w, 500, map[string]any{"status": "ERROR", "error": err.Error()})
			return
		}
		writeJSON(w, 200, map[string]any{"status": "OK", "sqlite_version": version, "sqlite_source_id": source})
	})
	mux.HandleFunc("/v1/jobs", func(w http.ResponseWriter, r *http.Request) {
		if !s.authorized(r) {
			writeJSON(w, 401, map[string]any{"code": "ACCESS_DENIED", "error": "write token required"})
			return
		}
		if r.Method != http.MethodPost {
			http.Error(w, "method", http.StatusMethodNotAllowed)
			return
		}
		var env map[string]any
		if err := decodeStrict(r.Body, &env); err != nil {
			writeJSON(w, 400, map[string]any{"code": "SCHEMA_INVALID", "error": err.Error()})
			return
		}
		receipt, err := s.Q.Submit(r.Context(), env)
		if err != nil {
			writeJSON(w, 400, map[string]any{"code": "SCHEMA_INVALID", "error": err.Error()})
			return
		}
		writeJSON(w, 202, receipt)
	})
	mux.HandleFunc("/v1/jobs/", func(w http.ResponseWriter, r *http.Request) {
		id := strings.TrimPrefix(r.URL.Path, "/v1/jobs/")
		status, err := s.Q.Status(r.Context(), id)
		if err != nil {
			writeJSON(w, 404, map[string]any{"code": "NOT_FOUND", "error": err.Error()})
			return
		}
		writeJSON(w, 200, status)
	})
	mux.HandleFunc("/v1/ledger", func(w http.ResponseWriter, r *http.Request) {
		limit := 200
		if v := r.URL.Query().Get("limit"); v != "" {
			if n, err := strconv.Atoi(v); err == nil && n >= 1 && n <= 200 {
				limit = n
			}
		}
		events, next, more, err := s.L.Page(r.Context(), r.URL.Query().Get("cursor"), limit)
		if err != nil {
			writeJSON(w, 400, map[string]any{"code": "SCHEMA_INVALID", "error": err.Error()})
			return
		}
		page := map[string]any{"events": events, "next_cursor": nil, "has_more": more}
		if next != "" {
			page["next_cursor"] = next
		}
		writeJSON(w, 200, page)
	})
	mux.HandleFunc("/v1/dev/params", func(w http.ResponseWriter, r *http.Request) {
		if !s.authorized(r) {
			writeJSON(w, 401, map[string]any{"code": "ACCESS_DENIED", "error": "write token required"})
			return
		}
		raw, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil || len(raw) == 0 {
			writeJSON(w, 400, map[string]any{"code": "SCHEMA_INVALID", "error": "empty params"})
			return
		}
		var probe map[string]any
		if err := json.Unmarshal(raw, &probe); err != nil {
			writeJSON(w, 400, map[string]any{"code": "SCHEMA_INVALID", "error": err.Error()})
			return
		}
		files := []artifacts.FileInput{{Path: "params.json", Data: raw}}
		indexHash := artifacts.IndexHash(files)
		manifest := map[string]any{
			"schema_version": "1.1.0",
			"artifact": map[string]any{
				"sha256": indexHash, "schema_id": "urn:qel:job:params", "schema_version": "1.1.0",
				"size_bytes": strconv.Itoa(len(raw)), "visibility": "DEVELOPMENT", "media_type": "application/json",
			},
			"producer_binary_hash": strings.Repeat("0", 64),
			"input_hashes":         []any{},
			"license_id":           "PROPRIETARY",
			"redistribution":       false,
			"created_at_ns":        strconv.FormatInt(time.Now().UnixNano(), 10),
			"complete":             true,
			"parent_hashes":        []any{},
			"row_count":            "1",
			"semantic_hash":        indexHash,
		}
		if _, err := s.A.Seal(r.Context(), s.L, manifest, files, nil); err != nil {
			writeJSON(w, 500, map[string]any{"code": "ARTIFACT_INCOMPLETE", "error": err.Error()})
			return
		}
		writeJSON(w, 200, map[string]any{"artifact": manifest["artifact"], "manifest_hash": indexHash})
	})
	mux.HandleFunc("/v1/dashboard", func(w http.ResponseWriter, r *http.Request) {
		snap, err := s.Dashboard(r.Context())
		if err != nil {
			writeJSON(w, 500, map[string]any{"code": "INTERNAL", "error": err.Error()})
			return
		}
		writeJSON(w, 200, snap)
	})
	mux.HandleFunc("/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		claims, err := s.M.Search(r.Context(), r.URL.Query().Get("q"), 50)
		if err != nil {
			writeJSON(w, 400, map[string]any{"code": "SCHEMA_INVALID", "error": err.Error()})
			return
		}
		writeJSON(w, 200, map[string]any{"claims": claims})
	})
	mux.HandleFunc("/v1/agent/tasks", func(w http.ResponseWriter, r *http.Request) {
		if !s.authorized(r) {
			writeJSON(w, 401, map[string]any{"code": "ACCESS_DENIED", "error": "write token required"})
			return
		}
		var body map[string]any
		if err := decodeStrict(r.Body, &body); err != nil {
			writeJSON(w, 400, map[string]any{"code": "SCHEMA_INVALID", "error": err.Error()})
			return
		}
		taskID, _ := body["task_id"].(string)
		action, _ := body["action"].(string)
		switch action {
		case "add":
			payload, _ := body["payload"].(map[string]any)
			if err := s.M.AddTask(r.Context(), taskID, payload); err != nil {
				writeJSON(w, 400, map[string]any{"code": "SCHEMA_INVALID", "error": err.Error()})
				return
			}
			writeJSON(w, 200, map[string]any{"task_id": taskID, "state": "QUEUED"})
		case "assign":
			gen, err := s.M.Assign(r.Context(), taskID, str(body["owner"], "agent"), 60*time.Second)
			if err != nil {
				writeJSON(w, 409, map[string]any{"code": "STALE_ASSIGNMENT", "error": err.Error()})
				return
			}
			writeJSON(w, 200, map[string]any{"task_id": taskID, "generation": gen, "state": "LEASED"})
		case "complete":
			gen, _ := body["generation"].(float64)
			if err := s.M.Complete(r.Context(), taskID, str(body["owner"], "agent"), int64(gen), str(body["status"], "DONE")); err != nil {
				writeJSON(w, 409, map[string]any{"code": "STALE_ASSIGNMENT", "error": err.Error()})
				return
			}
			writeJSON(w, 200, map[string]any{"task_id": taskID, "state": "DONE"})
		default:
			writeJSON(w, 400, map[string]any{"code": "SCHEMA_INVALID", "error": "unknown action"})
		}
	})
	mux.HandleFunc("/v1/events", s.sse)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(dashboardHTML))
	})
	return mux
}

func decodeStrict(body io.Reader, v any) error {
	dec := json.NewDecoder(body)
	dec.DisallowUnknownFields()
	return dec.Decode(v)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) sse(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", 500)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		snap, err := s.Dashboard(r.Context())
		if err != nil {
			return
		}
		payload, _ := json.Marshal(map[string]any{
			"event_id":   strconv.FormatInt(time.Now().Unix(), 10),
			"kind":       "HEARTBEAT",
			"projection": snap,
		})
		fmt.Fprintf(w, "data: %s\n\n", payload)
		flusher.Flush()
		select {
		case <-r.Context().Done():
			return
		case <-ticker.C:
		}
	}
}

// Dashboard projects live queue and ledger state.
func (s *Server) Dashboard(ctx context.Context) (map[string]any, error) {
	var queued, running, blocked int
	err := s.L.DB().QueryRowContext(ctx, `SELECT
		COALESCE(SUM(CASE WHEN state='QUEUED' THEN 1 ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN state IN ('LEASED','RUNNING') THEN 1 ELSE 0 END),0),
		COALESCE(SUM(CASE WHEN state LIKE 'WAITING%' THEN 1 ELSE 0 END),0) FROM jobs`).Scan(&queued, &running, &blocked)
	if err != nil {
		return nil, err
	}
	var cursor string
	if err := s.L.DB().QueryRowContext(ctx, `SELECT COALESCE(MAX(seq),0) FROM ledger_events`).Scan(&cursor); err != nil {
		return nil, err
	}
	snap := map[string]any{
		"generation":          0,
		"ledger_cursor":       cursor,
		"generated_ns":        strconv.FormatInt(time.Now().UnixNano(), 10),
		"trading_net_i":       "0",
		"lab_cost":            "0",
		"capital_break_even":  nil,
		"queue_counts":        map[string]any{"queued": queued, "running": running, "blocked": blocked},
		"oos_state":           "WAITING_FRESH_OOS",
		"alpha_next":          map[string]any{"num": "1", "den": "40"},
		"risk_state":          "RUNNING",
		"resource_profile_id": "RP-8G",
		"artifact_refs":       []any{},
	}
	if err := qelcontract.ValidateDef("DashboardSnapshot", snap); err != nil {
		return nil, err
	}
	return snap, nil
}

func str(v any, fallback string) string {
	if s, ok := v.(string); ok && s != "" {
		return s
	}
	return fallback
}
