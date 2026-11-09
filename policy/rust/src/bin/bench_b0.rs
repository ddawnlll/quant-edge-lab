//! B0 typed-specification benchmark driver (TASK-25).
//!
//! Measures PARSE + SCHEMA + TYPECHECK + HASH for the frozen 256-node
//! OPERATOR_SET_1_1 DAG. Two modes are timed separately:
//!
//! * `schema-only`  - JSON parse plus declared document/node field and operator
//!                    registry checks, without instantiating the typed DAG.
//! * `native`       - JSON parse, full `parse_strategy` (schema + arity + type +
//!                    parameter + clock/null/fit-scope validation) and engine
//!                    state construction.
//!
//! Both modes hash the canonical parsed form. This evaluator is interpreted:
//! there is no native/JIT compilation path, so the compilation counter is
//! structurally zero. The counter is still explicit so a future backend cannot
//! silently start compiling during B0.

use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Instant;

use qel_policy::{op_def, parse_strategy, Engine};
use serde_json::{json, Value};

const OPERATOR_SET_1_1: &[&str] = &[
    "OP-INPUT", "OP-CONST", "OP-ADD", "OP-SUB", "OP-MUL", "OP-DIV", "OP-LOG", "OP-EXP",
    "OP-ABS", "OP-LT", "OP-LE", "OP-GT", "OP-GE", "OP-AND", "OP-OR", "OP-NOT", "OP-LAG",
    "OP-SMA", "OP-SUM", "OP-MIN", "OP-MAX", "OP-EMA", "OP-STD", "OP-ER", "OP-PIVOT",
    "OP-QUANTILE",
];

const NODE_FIELDS: &[&str] = &[
    "node_id", "operator_id", "inputs", "params", "output_type", "fit_scope", "null_policy",
    "clock",
];

static mut NATIVE_JIT_COMPILATIONS: u64 = 0;

/// Records a compilation. No call site exists in this interpreter; the hook is
/// present so a compiler-backed path would have to increment it explicitly.
#[allow(dead_code)]
pub fn note_compilation() {
    // Safety: single-threaded benchmark driver.
    unsafe {
        NATIVE_JIT_COMPILATIONS += 1;
    }
}

fn compilation_count() -> u64 {
    unsafe { NATIVE_JIT_COMPILATIONS }
}

/// Minimal SHA-256 so the HASH phase does not add a dependency.
struct Sha256 {
    state: [u32; 8],
    buffer: [u8; 64],
    buffered: usize,
    length: u64,
}

const K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
    0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
    0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
    0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
    0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
    0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
    0xc67178f2,
];

impl Sha256 {
    fn new() -> Self {
        Sha256 {
            state: [
                0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c,
                0x1f83d9ab, 0x5be0cd19,
            ],
            buffer: [0; 64],
            buffered: 0,
            length: 0,
        }
    }

    fn compress(&mut self, block: &[u8]) {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([
                block[i * 4],
                block[i * 4 + 1],
                block[i * 4 + 2],
                block[i * 4 + 3],
            ]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let mut a = self.state;
        for i in 0..64 {
            let s1 = a[4].rotate_right(6) ^ a[4].rotate_right(11) ^ a[4].rotate_right(25);
            let ch = (a[4] & a[5]) ^ (!a[4] & a[6]);
            let t1 = a[7]
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a[0].rotate_right(2) ^ a[0].rotate_right(13) ^ a[0].rotate_right(22);
            let maj = (a[0] & a[1]) ^ (a[0] & a[2]) ^ (a[1] & a[2]);
            let t2 = s0.wrapping_add(maj);
            a[7] = a[6];
            a[6] = a[5];
            a[5] = a[4];
            a[4] = a[3].wrapping_add(t1);
            a[3] = a[2];
            a[2] = a[1];
            a[1] = a[0];
            a[0] = t1.wrapping_add(t2);
        }
        for i in 0..8 {
            self.state[i] = self.state[i].wrapping_add(a[i]);
        }
    }

    fn update(&mut self, mut data: &[u8]) {
        self.length += data.len() as u64;
        if self.buffered > 0 {
            let need = 64 - self.buffered;
            let take = need.min(data.len());
            self.buffer[self.buffered..self.buffered + take].copy_from_slice(&data[..take]);
            self.buffered += take;
            data = &data[take..];
            if self.buffered == 64 {
                let block = self.buffer;
                self.compress(&block);
                self.buffered = 0;
            }
        }
        while data.len() >= 64 {
            let (block, rest) = data.split_at(64);
            self.compress(block);
            data = rest;
        }
        if !data.is_empty() {
            self.buffer[..data.len()].copy_from_slice(data);
            self.buffered = data.len();
        }
    }

    fn finish(mut self) -> String {
        let bits = self.length * 8;
        self.update(&[0x80]);
        while self.buffered != 56 {
            self.update(&[0]);
        }
        self.update(&bits.to_be_bytes());
        let mut out = String::with_capacity(64);
        for word in self.state {
            out.push_str(&format!("{word:08x}"));
        }
        out
    }
}

fn sha256_hex(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(data);
    h.finish()
}

fn schema_only(value: &Value) -> Result<usize, String> {
    let object = value.as_object().ok_or("document is not an object")?;
    for key in ["schema_version", "feature_dag", "entry_policy", "exit_policy"] {
        if !object.contains_key(key) {
            return Err(format!("missing document field {key}"));
        }
    }
    let dag = value["feature_dag"]
        .as_array()
        .ok_or("feature_dag is not an array")?;
    if dag.is_empty() || dag.len() > 256 {
        return Err(format!("feature_dag size {}", dag.len()));
    }
    for node in dag {
        let obj = node.as_object().ok_or("node is not an object")?;
        for key in obj.keys() {
            if !NODE_FIELDS.contains(&key.as_str()) {
                return Err(format!("unknown node field {key}"));
            }
        }
        let op = node["operator_id"].as_str().ok_or("operator_id")?;
        if !OPERATOR_SET_1_1.contains(&op) {
            return Err(format!("operator outside OPERATOR_SET_1_1: {op}"));
        }
        if op_def(op).is_none() {
            return Err(format!("operator not registered: {op}"));
        }
        if node["node_id"].as_str().is_none() {
            return Err("node_id".to_string());
        }
    }
    Ok(dag.len())
}

/// Canonical hash over the parsed DAG: node order, ids, operators, inputs and
/// sorted params. This is the HASH phase and is identical for both modes.
fn hash_dag(value: &Value) -> String {
    let dag = value["feature_dag"].as_array().cloned().unwrap_or_default();
    let mut canonical = Vec::with_capacity(dag.len() * 96);
    for node in dag {
        canonical.extend_from_slice(node["node_id"].as_str().unwrap_or("").as_bytes());
        canonical.push(0);
        canonical.extend_from_slice(node["operator_id"].as_str().unwrap_or("").as_bytes());
        canonical.push(0);
        if let Some(inputs) = node["inputs"].as_array() {
            for i in inputs {
                canonical.extend_from_slice(i.as_str().unwrap_or("").as_bytes());
                canonical.push(1);
            }
        }
        canonical.push(2);
        if let Some(params) = node["params"].as_object() {
            let sorted: BTreeMap<&String, &Value> = params.iter().collect();
            for (k, v) in sorted {
                canonical.extend_from_slice(k.as_bytes());
                canonical.push(3);
                canonical.extend_from_slice(v.to_string().as_bytes());
                canonical.push(1);
            }
        }
        canonical.push(4);
    }
    sha256_hex(&canonical)
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    let rank = ((p / 100.0) * sorted.len() as f64).ceil() as usize;
    sorted[rank.clamp(1, sorted.len()) - 1]
}

fn stats(values: &[f64]) -> Value {
    if values.is_empty() {
        return json!({"median_ms": null, "p90_ms": null, "min_ms": null, "max_ms": null, "repeats": 0, "samples_ms": []});
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let median = if sorted.len() % 2 == 1 {
        sorted[sorted.len() / 2]
    } else {
        (sorted[sorted.len() / 2 - 1] + sorted[sorted.len() / 2]) / 2.0
    };
    json!({
        "median_ms": median,
        "p90_ms": percentile(&sorted, 90.0),
        "min_ms": sorted.first().copied(),
        "max_ms": sorted.last().copied(),
        "repeats": sorted.len(),
        "samples_ms": sorted,
    })
}

struct Args {
    policy: PathBuf,
    mode: String,
    repeats: usize,
    warmups: usize,
    out: Option<PathBuf>,
}

fn parse_args() -> Result<Args, String> {
    let mut args = Args {
        policy: PathBuf::new(),
        mode: "native".to_string(),
        repeats: 12,
        warmups: 2,
        out: None,
    };
    let mut it = std::env::args().skip(1);
    while let Some(flag) = it.next() {
        match flag.as_str() {
            "--policy" => args.policy = PathBuf::from(it.next().ok_or("--policy")?),
            "--mode" => args.mode = it.next().ok_or("--mode")?,
            "--repeats" => {
                args.repeats = it
                    .next()
                    .ok_or("--repeats")?
                    .parse()
                    .map_err(|_| "repeats")?
            }
            "--warmups" => {
                args.warmups = it
                    .next()
                    .ok_or("--warmups")?
                    .parse()
                    .map_err(|_| "warmups")?
            }
            "--out" => args.out = Some(PathBuf::from(it.next().ok_or("--out")?)),
            other => return Err(format!("unknown argument {other}")),
        }
    }
    if args.mode != "native" && args.mode != "schema-only" {
        return Err(format!("unknown mode {}", args.mode));
    }
    Ok(args)
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("argument error: {e}");
            return ExitCode::from(2);
        }
    };
    let raw = match fs::read(&args.policy) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("read {}: {e}", args.policy.display());
            return ExitCode::from(2);
        }
    };
    let corpus_hash = sha256_hex(&raw);

    let mut failed_attempts = Vec::new();
    let mut warm = Vec::new();
    let mut timed = Vec::new();
    let mut phases: Vec<[f64; 4]> = Vec::new();
    let mut nodes_seen = 0usize;
    let mut dag_hash = String::new();

    let total = args.warmups + args.repeats;
    for attempt in 0..total {
        let started = Instant::now();
        let parse_started = Instant::now();
        let value: Value = match serde_json::from_slice(&raw) {
            Ok(v) => v,
            Err(e) => {
                failed_attempts.push(json!({"attempt": attempt, "phase": "PARSE", "error": e.to_string()}));
                continue;
            }
        };
        let parse_ms = parse_started.elapsed().as_secs_f64() * 1000.0;

        let schema_started = Instant::now();
        let schema_nodes = match schema_only(&value) {
            Ok(n) => n,
            Err(e) => {
                failed_attempts.push(json!({"attempt": attempt, "phase": "SCHEMA", "error": e}));
                continue;
            }
        };
        nodes_seen = schema_nodes;
        let schema_ms = schema_started.elapsed().as_secs_f64() * 1000.0;

        let type_started = Instant::now();
        let typecheck_ok = if args.mode == "native" {
            match parse_strategy(&value) {
                Ok(strategy) => {
                    let engine = Engine::new(strategy);
                    // Touch the engine so construction cannot be optimized out.
                    let _bars = engine.bars;
                    true
                }
                Err(e) => {
                    failed_attempts.push(json!({"attempt": attempt, "phase": "TYPECHECK", "error": e.to_string()}));
                    continue;
                }
            }
        } else {
            true
        };
        let typecheck_ms = type_started.elapsed().as_secs_f64() * 1000.0;
        debug_assert!(typecheck_ok);

        let hash_started = Instant::now();
        dag_hash = hash_dag(&value);
        let hash_ms = hash_started.elapsed().as_secs_f64() * 1000.0;
        let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;

        phases.push([parse_ms, schema_ms, typecheck_ms, hash_ms]);
        if attempt < args.warmups {
            warm.push(elapsed_ms);
        } else {
            timed.push(elapsed_ms);
        }
    }

    let timings = stats(&timed);
    let warm_stats = stats(&warm);
    let phase_median = |idx: usize| -> f64 {
        if phases.is_empty() {
            return f64::NAN;
        }
        let mut column: Vec<f64> = phases.iter().map(|p| p[idx]).collect();
        column.sort_by(|a, b| a.partial_cmp(b).unwrap());
        column[column.len() / 2]
    };
    let p90 = timings["p90_ms"].as_f64().unwrap_or(f64::NAN);
    if timed.is_empty() {
        eprintln!("no successful timed repeat: {}", serde_json::to_string(&failed_attempts).unwrap());
        return ExitCode::from(1);
    }
    let target = 100.0;
    let passed = failed_attempts.is_empty() && p90 <= target && timed.len() >= 10;

    let report = json!({
        "kind": "TASK25_B0_TYPED_SPECIFICATION",
        "required_check": "B0",
        "corpus": args.policy.to_string_lossy(),
        "corpus_sha256": corpus_hash,
        "dag_sha256_canonical": dag_hash,
        "dag_nodes_measured": nodes_seen,
        "mode": args.mode,
        "modes_timed_separately": ["schema-only", "native"],
        "operator_set": "OPERATOR_SET_1_1",
        "include": ["PARSE", "SCHEMA", "TYPECHECK", "HASH"],
        "warmups": args.warmups,
        "timed_repeats": args.repeats,
        "warm": warm_stats,
        "timed": timings,
        "phase_median_ms": {
            "parse": phase_median(0),
            "schema": phase_median(1),
            "typecheck_and_engine": phase_median(2),
            "hash": phase_median(3),
        },
        "metric": {
            "name": "p90_ms",
            "value": p90,
            "target": target,
            "operator": "LE",
            "passed": p90 <= target,
        },
        "native_jit_compilations": compilation_count(),
        "compilation_path": "INTERPRETED_NO_NATIVE_JIT_PATH",
        "failed_attempts": failed_attempts,
        "verdict": if passed { "PASS" } else { "FAIL" },
    });
    let rendered = serde_json::to_string_pretty(&report).unwrap();
    match args.out {
        Some(path) => {
            if let Err(e) = fs::write(&path, rendered.clone() + "\n") {
                eprintln!("write {}: {e}", path.display());
                return ExitCode::from(2);
            }
        }
        None => println!("{rendered}"),
    }
    if passed {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}
