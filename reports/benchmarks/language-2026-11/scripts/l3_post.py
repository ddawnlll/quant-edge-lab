#!/usr/bin/env python3
"""L3 post-baseline: after runner cold build, measure warm-noop + one
semantic-ish edit rebuild + disk + binary sizes, record to raw/l3.jsonl.
Run once when runner cold build is DONE (log has 'Finished')."""
import json, os, subprocess, time

ROOT = "/home/quant/quant/bench-lang-2026-11"
T = os.path.join(ROOT, "work/l3/runner-target")
MAN = "/home/quant/quant/quant-edge-lab/native/runner/Cargo.toml"

def run(cmd, tag, timeout=1200):
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        wall = time.perf_counter() - t0
        out = (p.stdout + p.stderr)[-300:]
        return {"tag": tag, "wall_s": round(wall, 3), "exit": p.returncode, "tail": out}
    except subprocess.TimeoutExpired:
        return {"tag": tag, "wall_s": round(time.perf_counter() - t0, 3),
                "exit": 124, "tail": "timeout"}

recs = []
env = dict(os.environ, CARGO_TARGET_DIR=T)
r = run(["cargo", "build", "--offline", "--locked", "--manifest-path", MAN, "--target-dir", T], "runner-warm-noop")
recs.append(r); print(r, flush=True)
# one-line real edit: bump a comment-free semantic token in policy? NO: production code
# must not be touched. Instead measure incremental cost of touching a policy source
# via --fingerprint force: touch policy file mtime only (no content change).
import pathlib
pf = pathlib.Path("/home/quant/quant/quant-edge-lab/policy/rust/src/lib.rs")
st = pf.stat()
os.utime(pf, (st.st_atime, st.st_mtime + 5))
r = run(["cargo", "build", "--offline", "--locked", "--manifest-path", MAN, "--target-dir", T], "runner-rebuild-after-policy-touch")
recs.append(r); print(r, flush=True)
# cold target disk + binary sizes
tot = 0
for dp, dn, fn in os.walk(T):
    for f in fn:
        try:
            tot += os.lstat(os.path.join(dp, f)).st_blocks * 512
        except Exception:
            pass
recs.append({"tag": "runner-target-bytes", "allocated_bytes": tot})
for dp, dn, fn in os.walk(T):
    for f in fn:
        if os.path.basename(f) == "qel-native-runner":
            recs.append({"tag": "runner-binary", "path": os.path.join(dp, f),
                         "bytes": os.path.getsize(os.path.join(dp, f))})
print(recs[-2:], flush=True)
with open(os.path.join(ROOT, "raw/l3.jsonl"), "a") as f:
    for r in recs:
        f.write(json.dumps(r) + "\n")
print("DONE")
