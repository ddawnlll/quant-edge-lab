//! Canonical JSON identity, exact decimals and run keys for QEL.

use anyhow::{anyhow, bail, Result};
use serde_json::Value;
use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;

pub const MAX_SAFE: u64 = 9_007_199_254_740_991;
pub const RUN_DOMAIN: &str = "QEL.RUN.1.1\0";
pub const LEDGER_DOMAIN: &str = "QEL.LEDGER.1.1\0";

pub const RUN_KEY_KEYS: [&str; 13] = [
    "code_tree_hash",
    "dependency_lock_hash",
    "binary_hash",
    "dataset_manifest_hash",
    "strategy_hash",
    "venue_hash",
    "split_hash",
    "seed_protocol_hash",
    "environment_hash",
    "protocol_hash",
    "capital_profile_hash",
    "latency_hash",
    "control_spec_hash",
];

fn check_text(s: &str) -> Result<()> {
    if s.nfc().collect::<String>() != s {
        bail!("non-NFC text");
    }
    Ok(())
}

fn quote(s: &str, out: &mut String) -> Result<()> {
    check_text(s)?;
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{0008}' => out.push_str("\\b"),
            '\u{000c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    Ok(())
}

fn number(n: &serde_json::Number) -> Result<String> {
    if let Some(i) = n.as_i64() {
        if i.unsigned_abs() > MAX_SAFE {
            bail!("integer outside JCS safe range");
        }
        return Ok(i.to_string());
    }
    if let Some(u) = n.as_u64() {
        if u > MAX_SAFE {
            bail!("integer outside JCS safe range");
        }
        return Ok(u.to_string());
    }
    bail!("floating point forbidden in canonical identity objects")
}

fn utf16_key(a: &str, b: &str) -> std::cmp::Ordering {
    a.encode_utf16().cmp(b.encode_utf16())
}

fn encode(v: &Value, out: &mut String) -> Result<()> {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::String(s) => quote(s, out)?,
        Value::Number(n) => out.push_str(&number(n)?),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                encode(item, out)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort_by(|a, b| utf16_key(a, b));
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                quote(k, out)?;
                out.push(':');
                encode(&map[*k], out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

pub fn canonical_bytes(v: &Value) -> Result<Vec<u8>> {
    let mut out = String::new();
    encode(v, &mut out)?;
    Ok(out.into_bytes())
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    format!("{:x}", h.finalize())
}

pub fn hash_jcs(v: &Value) -> Result<String> {
    Ok(sha256_hex(&canonical_bytes(v)?))
}

pub fn domain_hash(prefix: &str, v: &Value) -> Result<String> {
    let mut h = Sha256::new();
    h.update(prefix.as_bytes());
    h.update(canonical_bytes(v)?);
    Ok(format!("{:x}", h.finalize()))
}

/// Parse a canonical decimal into (coefficient, scale). Coefficients beyond
/// i128 are rejected for the identity domain.
pub fn parse_decimal(s: &str) -> Result<(i128, u32)> {
    let bytes = s.as_bytes();
    if bytes.is_empty() {
        bail!("empty decimal");
    }
    let mut idx = 0usize;
    let neg = bytes[0] == b'-';
    if neg {
        idx = 1;
    }
    if idx >= bytes.len() || !bytes[idx].is_ascii_digit() {
        bail!("non-canonical decimal");
    }
    if bytes[idx] == b'0' && idx + 1 < bytes.len() && bytes[idx + 1].is_ascii_digit() {
        bail!("leading zero");
    }
    let start = idx;
    while idx < bytes.len() && bytes[idx].is_ascii_digit() {
        idx += 1;
    }
    let whole = &s[start..idx];
    let mut frac = "";
    if idx < bytes.len() {
        if bytes[idx] != b'.' {
            bail!("non-canonical decimal");
        }
        idx += 1;
        let fstart = idx;
        while idx < bytes.len() && bytes[idx].is_ascii_digit() {
            idx += 1;
        }
        if idx != bytes.len() {
            bail!("non-canonical decimal");
        }
        frac = &s[fstart..idx];
        if frac.is_empty() || frac.ends_with('0') {
            bail!("non-canonical decimal");
        }
    }
    if neg && whole == "0" && (frac.is_empty() || frac.chars().all(|c| c == '0')) {
        bail!("negative zero");
    }
    let digits = format!("{whole}{frac}");
    let coeff: i128 = digits.parse().map_err(|_| anyhow!("decimal coefficient overflow"))?;
    let coeff = if neg { -coeff } else { coeff };
    Ok((coeff, frac.len() as u32))
}

pub fn format_decimal(coeff: i128, scale: u32) -> String {
    if scale == 0 {
        return coeff.to_string();
    }
    let sign = if coeff < 0 { "-" } else { "" };
    let digits = coeff.unsigned_abs().to_string();
    let digits = format!("{digits:0>width$}", width = scale as usize + 1);
    let split = digits.len() - scale as usize;
    let frac = digits[split..].trim_end_matches('0');
    if frac.is_empty() {
        format!("{sign}{}", &digits[..split])
    } else {
        format!("{sign}{}.{}", &digits[..split], frac)
    }
}

/// Parse a canonical integer inside signed 128-bit bounds.
pub fn parse_int128(s: &str) -> Result<i128> {
    if s == "-0" || s.is_empty() {
        bail!("negative zero or empty integer");
    }
    let neg = s.starts_with('-');
    let body = if neg { &s[1..] } else { s };
    if body.is_empty() || !body.bytes().all(|b| b.is_ascii_digit()) {
        bail!("non-canonical integer");
    }
    if body.len() > 1 && body.starts_with('0') {
        bail!("leading zero");
    }
    let v: i128 = s.parse().map_err(|_| anyhow!("integer outside declared domain"))?;
    Ok(v)
}

pub fn run_key(material: &serde_json::Map<String, Value>) -> Result<String> {
    if material.len() != RUN_KEY_KEYS.len() {
        bail!("run-key field count mismatch");
    }
    let mut out = serde_json::Map::new();
    for k in RUN_KEY_KEYS {
        let v = material.get(k).and_then(|v| v.as_str()).ok_or_else(|| anyhow!("missing {k}"))?;
        if v.len() != 64 || !v.bytes().all(|b| b.is_ascii_hexdigit()) {
            bail!("run-key field {k} is not a 64-hex digest");
        }
        out.insert(k.to_string(), Value::String(v.to_string()));
    }
    domain_hash(RUN_DOMAIN, &Value::Object(out))
}

pub fn ledger_event_hash(event: &serde_json::Map<String, Value>, previous: &str) -> Result<String> {
    let mut out = event.clone();
    out.remove("event_hash");
    out.insert("previous_hash".into(), Value::String(previous.to_string()));
    domain_hash(LEDGER_DOMAIN, &Value::Object(out))
}
