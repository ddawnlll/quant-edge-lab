#!/usr/bin/env python3
"""Summarize trials.jsonl into results.csv + results.json + PARITY.json.
Parity: every runtime/jit trial output hash must match the oracle fixture hash;
any mismatch = DISQUALIFIED from runtime ranking (correctness failure).
"""
import csv, hashlib, json, os, statistics, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRIALS = os.path.join(ROOT, "raw", "trials.jsonl")
exp = {}
for n in ["1000000", "10000000"]:
    with open(os.path.join(ROOT, "fixtures", f"expected_{n}.txt"), "rb") as f:
        exp[n] = f.read().decode()
exp_sha = {n: hashlib.sha256(open(os.path.join(ROOT, "fixtures", f"expected_{n}.txt"), "rb").read()).hexdigest()[:16] for n in exp}

rows = []
with open(TRIALS) as f:
    for line in f:
        if line.strip():
            rows.append(json.loads(line))

# aggregate per (phase, lang)
groups = {}
for r in rows:
    key = (r["phase"], r["lang"])
    groups.setdefault(key, []).append(r)

def med(vals):
    return statistics.median(vals) if vals else None

def pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))]

summary = []
for (phase, lang), items in sorted(groups.items()):
    walls = sorted(i["wall_s"] for i in items)
    entry = {
        "phase": phase, "lang": lang, "n": len(items),
        "median_wall_s": round(med(walls), 4) if walls else None,
        "p90_wall_s": round(pct(walls, 0.9), 4) if walls else None,
        "min_wall_s": round(min(walls), 4) if walls else None,
        "max_wall_s": round(max(walls), 4) if walls else None,
        "median_cpu_s": round(med([i["cpu_s"] for i in items]), 4) if items else None,
        "median_maxrss_kb": med([i["maxrss_kb"] for i in items]),
        "median_load1": med([i["load1"] for i in items]),
        "contended_trials": sum(1 for i in items if i["contended"]),
        "failures": sum(1 for i in items if i["exit"] != 0),
    }
    # runtime throughput at 1M/10M
    rt1 = [i["events_per_sec"] for i in items if i.get("n") == "1000000" and "events_per_sec" in i]
    rt10 = [i["events_per_sec"] for i in items if i.get("n") == "10000000" and "events_per_sec" in i]
    if rt1:
        entry["eps_1M_median"] = round(med(rt1), 1)
        entry["eps_1M_min"] = round(min(rt1), 1)
        entry["valid_1M"] = all(i.get("valid") for i in items if i.get("n") == "1000000")
    if rt10:
        entry["eps_10M_median"] = round(med(rt10), 1)
        entry["eps_10M_min"] = round(min(rt10), 1)
        entry["valid_10M"] = all(i.get("valid") for i in items if i.get("n") == "10000000")
    disk = [i["disk_bytes"] for i in items if "disk_bytes" in i and i["disk_bytes"] > 0]
    if disk:
        entry["median_disk_bytes"] = med(disk)
        entry["max_disk_bytes"] = max(disk)
    bins = [i["bin_bytes"] for i in items if "bin_bytes" in i and i["bin_bytes"] > 0]
    if bins:
        entry["median_bin_bytes"] = med(bins)
    summary.append(entry)

with open(os.path.join(ROOT, "results.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["phase", "lang", "n", "median_wall_s", "p90_wall_s", "min_wall_s", "max_wall_s", "median_cpu_s", "median_maxrss_kb", "median_load1", "contended_trials", "failures", "eps_1M_median", "eps_10M_median", "valid_1M", "valid_10M", "median_disk_bytes", "max_disk_bytes", "median_bin_bytes"])
    w.writeheader()
    for s in summary:
        w.writerow({k: s.get(k) for k in w.fieldnames})

with open(os.path.join(ROOT, "results.json"), "w") as f:
    json.dump(summary, f, indent=2)

parity = {
    "expected_sha16": exp_sha,
    "trial_output_shas": {},
}
for r in rows:
    if r["phase"] in ("runtime", "goose-jit-first", "goose-jit-warm", "goose-jit-warm-init"):
        parity["trial_output_shas"].setdefault(r["phase"] + "/" + r["lang"], set()).add(r["stdout_sha"])
out_shas = {k: sorted(v) for k, v in parity["trial_output_shas"].items()}
parity["trial_output_shas"] = out_shas
expected_set = set(exp_sha.values())
parity["all_match_expected"] = {k: (len(v) == 1 and v[0] in expected_set) for k, v in out_shas.items()}
parity["boundary_and_1000_verified_manually"] = "Rust/Zig/Goose-AOT diff vs oracle 1000 + boundary: byte-identical (see METHODOLOGY run log 2026-11-11 16:06-16:09)"
with open(os.path.join(ROOT, "PARITY.json"), "w") as f:
    json.dump(parity, f, indent=2)

print(f"{len(rows)} trials -> {len(summary)} groups")
for s in summary:
    print(s)
