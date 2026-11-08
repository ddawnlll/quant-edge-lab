package qelcontract

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
)

// RunDomain is the QEL run-key domain separator.
const RunDomain = "QEL.RUN.1.1\x00"

// LedgerDomain is the QEL ledger-event domain separator.
const LedgerDomain = "QEL.LEDGER.1.1\x00"

var runKeyKeys = []string{
	"code_tree_hash", "dependency_lock_hash", "binary_hash", "dataset_manifest_hash",
	"strategy_hash", "venue_hash", "split_hash", "seed_protocol_hash",
	"environment_hash", "protocol_hash", "capital_profile_hash", "latency_hash",
	"control_spec_hash",
}

// RunKey computes the declared 13-field run identity. Attempt IDs are excluded.
func RunKey(material map[string]any) (string, error) {
	if len(material) != len(runKeyKeys) {
		return "", fmt.Errorf("run-key fields mismatch: got %d", len(material))
	}
	out := make(map[string]any, len(runKeyKeys))
	for _, k := range runKeyKeys {
		v, ok := material[k].(string)
		if !ok || len(v) != 64 || !isHex(v) {
			return "", fmt.Errorf("run-key field %s is not a 64-hex digest", k)
		}
		out[k] = v
	}
	return DomainHash(RunDomain, out)
}

// LedgerEventHash chains the previous hash into the canonical event payload.
func LedgerEventHash(event map[string]any, previous string) (string, error) {
	payload := make(map[string]any, len(event)+1)
	for k, v := range event {
		if k != "event_hash" {
			payload[k] = v
		}
	}
	payload["previous_hash"] = previous
	return DomainHash(LedgerDomain, payload)
}

func isHex(s string) bool {
	_, err := hex.DecodeString(s)
	return err == nil
}

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
