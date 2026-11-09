// Package artifacts implements an atomic content-addressed artifact store.
package artifacts

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"time"

	"github.com/ddawnlll/quant-edge-lab/contracts/go/qelcontract"
	"github.com/ddawnlll/quant-edge-lab/labd/ledger"
)

type FileInput struct {
	Path string
	Data []byte
}

type FaultHook func(point string) error

type Store struct {
	Root string
}

func New(root string) (*Store, error) {
	for _, dir := range []string{"blobs", "manifests", "tmp", "staging"} {
		if err := os.MkdirAll(filepath.Join(root, dir), 0o750); err != nil {
			return nil, err
		}
	}
	return &Store{Root: root}, nil
}

type fileRecord struct {
	Path   string `json:"path"`
	Sha256 string `json:"sha256"`
	Size   string `json:"size_bytes"`
}

func hashBytes(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func (s *Store) blobPath(hash string) string {
	return filepath.Join(s.Root, "blobs", hash[:2], hash)
}

func (s *Store) manifestPath(hash string) string {
	return filepath.Join(s.Root, "manifests", hash+".json")
}

func fsyncDir(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}

func (s *Store) closeBlob(hash string, data []byte, hook FaultHook) (string, error) {
	blob := s.blobPath(hash)
	if _, err := os.Stat(blob); err == nil {
		if existing, err := os.ReadFile(blob); err == nil && hashBytes(existing) == hash {
			return hash, nil
		}
	}
	if err := os.MkdirAll(filepath.Dir(blob), 0o750); err != nil {
		return "", err
	}
	tmp, err := os.CreateTemp(filepath.Join(s.Root, "tmp"), "blob-*")
	if err != nil {
		return "", err
	}
	tmpName := tmp.Name()
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return "", err
	}
	if hook != nil {
		if err := hook("WRITE_TEMP"); err != nil {
			tmp.Close()
			os.Remove(tmpName)
			return "", err
		}
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return "", err
	}
	if hook != nil {
		if err := hook("FILE_FSYNC"); err != nil {
			tmp.Close()
			os.Remove(tmpName)
			return "", err
		}
	}
	tmp.Close()
	if hook != nil {
		if err := hook("RENAME"); err != nil {
			os.Remove(tmpName)
			return "", err
		}
	}
	if err := os.Rename(tmpName, blob); err != nil {
		os.Remove(tmpName)
		return "", err
	}
	if hook != nil {
		if err := hook("DIR_FSYNC"); err != nil {
			return "", err
		}
	}
	if err := fsyncDir(filepath.Dir(blob)); err != nil {
		return "", err
	}
	return hash, nil
}

// Seal publishes files and a manifest. The caller supplies the complete
// manifest; its artifact.sha256 must equal the content index hash.
func (s *Store) Seal(ctx context.Context, l *ledger.Ledger, manifest map[string]any, files []FileInput, hook FaultHook) (map[string]any, error) {
	started := time.Now()
	fail := func(point string, err error) (map[string]any, error) {
		usage, _ := json.Marshal(map[string]any{
			"attempt_id":        "SEAL-" + strconv.FormatInt(started.UnixNano(), 10),
			"wall_ns":           strconv.FormatInt(time.Since(started).Nanoseconds(), 10),
			"cpu_ns":            "0",
			"cgroup_peak_bytes": "0",
			"rss_peak_bytes":    "0",
			"pss_peak_bytes":    nil,
			"read_bytes":        "0",
			"written_bytes":     "0",
		})
		artifactID := "ARTIFACT-UNKNOWN"
		if art, ok := manifest["artifact"].(map[string]any); ok {
			if sha, ok := art["sha256"].(string); ok && sha != "" {
				artifactID = sha
			}
		}
		_, _ = l.DB().ExecContext(context.Background(), `INSERT INTO seal_failures(artifact_id,point,message,usage_json,created_ns) VALUES(?,?,?,?,?)`,
			artifactID, point, err.Error(), string(usage), strconv.FormatInt(time.Now().UnixNano(), 10))
		return nil, err
	}

	if err := qelcontract.ValidateDef("ArtifactManifest", manifest); err != nil {
		return fail("VALIDATE", err)
	}
	if hook != nil {
		if err := hook("VALIDATE"); err != nil {
			return fail("VALIDATE", err)
		}
	}
	records := make([]fileRecord, 0, len(files))
	for _, f := range files {
		hash := hashBytes(f.Data)
		if _, err := s.closeBlob(hash, f.Data, hook); err != nil {
			return fail("BLOB", err)
		}
		records = append(records, fileRecord{Path: f.Path, Sha256: hash, Size: strconv.Itoa(len(f.Data))})
	}
	sort.Slice(records, func(i, j int) bool { return records[i].Path < records[j].Path })
	index, err := json.Marshal(records)
	if err != nil {
		return fail("VALIDATE", err)
	}
	packageHash := hashBytes(index)
	artifact, _ := manifest["artifact"].(map[string]any)
	if artifact == nil || artifact["sha256"] != packageHash {
		return fail("VALIDATE", fmt.Errorf("artifact.sha256 must equal content index hash %s", packageHash))
	}
	// Exact duplicate seals are idempotent and do not append a second event.
	var existingEvent string
	if err := l.DB().QueryRowContext(ctx, `SELECT manifest_hash,event_hash FROM artifacts WHERE artifact_id=?`, packageHash).Scan(new(string), &existingEvent); err == nil {
		return map[string]any{
			"artifact_id": packageHash, "manifest_hash": packageHash,
			"file_count": len(files), "ledger_event_hash": existingEvent, "reused": true,
		}, nil
	}
	manifestBytes, err := json.Marshal(manifest)
	if err != nil {
		return fail("VALIDATE", err)
	}
	mPath := s.manifestPath(packageHash)
	if err := os.MkdirAll(filepath.Dir(mPath), 0o750); err != nil {
		return fail("RENAME", err)
	}
	mTmp, err := os.CreateTemp(filepath.Join(s.Root, "tmp"), "manifest-*")
	if err != nil {
		return fail("WRITE_TEMP", err)
	}
	mTmpName := mTmp.Name()
	if _, err := mTmp.Write(manifestBytes); err != nil {
		mTmp.Close()
		os.Remove(mTmpName)
		return fail("WRITE_TEMP", err)
	}
	if err := mTmp.Sync(); err != nil {
		mTmp.Close()
		os.Remove(mTmpName)
		return fail("FILE_FSYNC", err)
	}
	if hook != nil {
		if err := hook("FILE_FSYNC"); err != nil {
			mTmp.Close()
			os.Remove(mTmpName)
			return fail("FILE_FSYNC", err)
		}
	}
	mTmp.Close()
	if hook != nil {
		if err := hook("RENAME"); err != nil {
			os.Remove(mTmpName)
			return fail("RENAME", err)
		}
	}
	if err := os.Rename(mTmpName, mPath); err != nil {
		os.Remove(mTmpName)
		return fail("RENAME", err)
	}
	if hook != nil {
		if err := hook("DIR_FSYNC"); err != nil {
			return fail("DIR_FSYNC", err)
		}
	}
	if err := fsyncDir(filepath.Dir(mPath)); err != nil {
		return fail("DIR_FSYNC", err)
	}

	if hook != nil {
		if err := hook("BEFORE_SQL_COMMIT"); err != nil {
			return fail("BEFORE_SQL_COMMIT", err)
		}
	}
	tx, err := l.DB().BeginTx(ctx, nil)
	if err != nil {
		return fail("SQL_COMMIT", err)
	}
	defer tx.Rollback()
	artifactID := packageHash
	_, err = tx.ExecContext(ctx, `INSERT INTO artifacts(artifact_id,manifest_json,manifest_hash,event_hash,created_ns) VALUES(?,?,?,?,?)`,
		artifactID, string(manifestBytes), packageHash, "", strconv.FormatInt(time.Now().UnixNano(), 10))
	if err != nil {
		tx.Rollback()
		return fail("SQL_COMMIT", err)
	}
	for _, rec := range records {
		size, _ := strconv.ParseInt(rec.Size, 10, 64)
		if _, err := tx.ExecContext(ctx, `INSERT INTO artifact_files(manifest_hash,path,sha256,size_bytes) VALUES(?,?,?,?)`,
			packageHash, rec.Path, rec.Sha256, size); err != nil {
			tx.Rollback()
			return fail("SQL_COMMIT", err)
		}
	}
	ev, err := l.AppendTx(ctx, tx, ledger.Input{
		EventType:    "ArtifactSealed",
		Payload:      map[string]any{"artifact_id": artifactID, "manifest_hash": packageHash, "file_count": strconv.Itoa(len(files))},
		ArtifactRefs: []any{artifact},
	})
	if err != nil {
		tx.Rollback()
		return fail("SQL_COMMIT", err)
	}
	if _, err := tx.ExecContext(ctx, `UPDATE artifacts SET event_hash=? WHERE artifact_id=?`, ev.EventHash, artifactID); err != nil {
		tx.Rollback()
		return fail("SQL_COMMIT", err)
	}
	if err := tx.Commit(); err != nil {
		return fail("SQL_COMMIT", err)
	}
	if hook != nil {
		if err := hook("AFTER_SQL_COMMIT"); err != nil {
			return fail("AFTER_SQL_COMMIT", err)
		}
	}
	return map[string]any{
		"artifact_id":       artifactID,
		"manifest_hash":     packageHash,
		"file_count":        len(files),
		"ledger_event_hash": ev.EventHash,
	}, nil
}

// RecoveryReport is the crash projection after a seal failure matrix.
type RecoveryReport struct {
	OrphanFinals      int  `json:"orphan_final_count"`
	PartialPackages   int  `json:"partial_package_pass_count"`
	AckedCommitLost   int  `json:"acked_commit_lost_count"`
	FailureUsageCount int  `json:"failure_usage_retained_count"`
	TemporaryFiles    int  `json:"temporary_file_count"`
	OrphanFinalFound  bool `json:"orphan_final_detected"`
	FailureRetained   bool `json:"failure_usage_retained"`
}

// Scan reconciles manifests, blobs and the database.
func (s *Store) Scan(ctx context.Context, l *ledger.Ledger) (RecoveryReport, error) {
	var report RecoveryReport
	entries, err := os.ReadDir(filepath.Join(s.Root, "manifests"))
	if err != nil {
		return report, err
	}
	known := map[string]bool{}
	for _, e := range entries {
		if e.IsDir() || filepath.Ext(e.Name()) != ".json" {
			continue
		}
		hash := e.Name()[:len(e.Name())-5]
		var manifest map[string]any
		raw, err := os.ReadFile(filepath.Join(s.Root, "manifests", e.Name()))
		if err != nil {
			return report, err
		}
		if err := json.Unmarshal(raw, &manifest); err != nil {
			report.PartialPackages++
			continue
		}
		complete, _ := manifest["complete"].(bool)
		ok := complete
		var rows *sql.Rows
		rows, err = l.DB().QueryContext(ctx, `SELECT sha256 FROM artifact_files WHERE manifest_hash=?`, hash)
		if err != nil {
			return report, err
		}
		count := 0
		for rows.Next() {
			var sha string
			if err := rows.Scan(&sha); err != nil {
				rows.Close()
				return report, err
			}
			count++
			if _, err := os.Stat(s.blobPath(sha)); err != nil {
				ok = false
			}
		}
		rows.Close()
		if count == 0 {
			known[hash] = false
		} else {
			known[hash] = true
		}
		if complete && !ok {
			report.PartialPackages++
		}
	}
	for _, inDB := range known {
		if !inDB {
			report.OrphanFinals++
		}
	}
	rows, err := l.DB().QueryContext(ctx, `SELECT manifest_hash FROM artifacts`)
	if err != nil {
		return report, err
	}
	for rows.Next() {
		var hash string
		if err := rows.Scan(&hash); err != nil {
			rows.Close()
			return report, err
		}
		if _, err := os.Stat(s.manifestPath(hash)); err != nil {
			report.AckedCommitLost++
		}
	}
	rows.Close()
	if entries, err := os.ReadDir(filepath.Join(s.Root, "tmp")); err == nil {
		report.TemporaryFiles = len(entries)
	}
	if err := l.DB().QueryRowContext(ctx, `SELECT COUNT(*) FROM seal_failures`).Scan(&report.FailureUsageCount); err != nil {
		return report, err
	}
	report.OrphanFinalFound = report.OrphanFinals > 0
	report.FailureRetained = report.FailureUsageCount > 0
	return report, nil
}

// ReadFile returns a sealed blob exactly when its hash matches.
func (s *Store) ReadFile(hash string) ([]byte, error) {
	if len(hash) != 64 {
		return nil, errors.New("invalid hash")
	}
	raw, err := os.ReadFile(s.blobPath(hash))
	if err != nil {
		return nil, err
	}
	if hashBytes(raw) != hash {
		return nil, errors.New("blob hash mismatch")
	}
	return raw, nil
}

// CopyTo streams a verified blob into w.
func (s *Store) CopyTo(hash string, w io.Writer) error {
	raw, err := s.ReadFile(hash)
	if err != nil {
		return err
	}
	_, err = w.Write(raw)
	return err
}

// IndexHash returns the canonical content index hash for a set of files.
func IndexHash(files []FileInput) string {
	records := make([]fileRecord, 0, len(files))
	for _, f := range files {
		records = append(records, fileRecord{Path: f.Path, Sha256: hashBytes(f.Data), Size: strconv.Itoa(len(f.Data))})
	}
	sort.Slice(records, func(i, j int) bool { return records[i].Path < records[j].Path })
	index, _ := json.Marshal(records)
	return hashBytes(index)
}

// ReadArtifactFile resolves one named file inside a sealed artifact package.
func (s *Store) ReadArtifactFile(ctx context.Context, l *ledger.Ledger, artifactID, path string) ([]byte, error) {
	var sha string
	if err := l.DB().QueryRowContext(ctx, `SELECT sha256 FROM artifact_files WHERE manifest_hash=? AND path=?`, artifactID, path).Scan(&sha); err != nil {
		return nil, err
	}
	return s.ReadFile(sha)
}
