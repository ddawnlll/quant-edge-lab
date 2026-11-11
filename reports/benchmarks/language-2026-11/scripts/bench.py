#!/usr/bin/env python3
"""L1 benchmark matrix runner. Serial, interleaved order, JSONL trials.

Usage: bench.py [phase_filter ...]  (no filter = all phases)
Phases: cold, warm, jit, runtime, edit, dep
All measurements go to raw/trials.jsonl. Rerunning appends; use fresh file per run.
"""
import hashlib, json, os, random, resource, shutil, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "raw")
os.makedirs(RAW, exist_ok=True)
TRIALS = os.path.join(RAW, "trials.jsonl")
SRC = os.path.join(ROOT, "sources")
WORK = os.path.join(ROOT, "work")
os.makedirs(WORK, exist_ok=True)

SEED = 20261111

def now_load():
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except Exception:
        return -1.0

def rss_kb_children():
    try:
        return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    except Exception:
        return -1

def cpu_s_children():
    try:
        u = resource.getrusage(resource.RUSAGE_CHILDREN)
        return u.ru_utime + u.ru_stime
    except Exception:
        return -1.0

def dir_bytes(path):
    total = 0
    if not os.path.exists(path):
        return 0
    for dp, dn, fn in os.walk(path):
        for f in fn:
            try:
                st = os.lstat(os.path.join(dp, f))
                total += getattr(st, "st_blocks", 0) * 512
            except Exception:
                pass
    return total

def clean(d):
    if os.path.exists(d):
        shutil.rmtree(d)

def run_cmd(cmd, env_extra=None, cwd=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    t0 = time.perf_counter()
    c0 = cpu_s_children()
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd)
    wall = time.perf_counter() - t0
    cpu = cpu_s_children() - c0 if cpu_s_children() >= 0 else -1.0
    return p, wall, cpu

def record(phase, lang, rep, cmd, wall, cpu, p, extra=None):
    out = (p.stdout or "")
    h = hashlib.sha256(out.encode()).hexdigest()[:16] if out else ""
    r = {
        "phase": phase, "lang": lang, "rep": rep, "cmd": " ".join(cmd),
        "wall_s": round(wall, 4), "cpu_s": round(cpu, 4),
        "maxrss_kb": rss_kb_children(),
        "exit": p.returncode, "stdout_sha": h,
        "load1": round(now_load(), 2),
        "contended": now_load() > 4.5,
        "stderr_tail": (p.stderr or "")[-300:],
    }
    r.update(extra or {})
    with open(TRIALS, "a") as f:
        f.write(json.dumps(r) + "\n")
    return r

def file_size(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return -1

def sha_file(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            h.update(f.read())
        return h.hexdigest()
    except Exception:
        return ""

# ---------- build command factories ----------
def rust_build(workdir, release):
    cmd = ["cargo", "build", "--offline", "--manifest-path", os.path.join(SRC, "rust", "Cargo.toml")]
    if release:
        cmd.append("--release")
    env = {"CARGO_TARGET_DIR": os.path.join(workdir, "target")}
    return cmd, env

def zig_build(cachedir, gcachedir, opt):
    cmd = ["zig", "build-exe", os.path.join(SRC, "zig", "main.zig"),
           "-O", opt, "--cache-dir", cachedir, "--global-cache-dir", gcachedir,
           "-femit-bin=" + os.path.join(cachedir, "l1kernel")]
    return cmd, {}

def goose_build(ghome, out, debug=False):
    cmd = ["goose", "build", "--cc", "clang", "-o", out, os.path.join(SRC, "goose", "main.goose")]
    if debug:
        cmd.append("--debug")
    return cmd, {"GOOSE_HOME": ghome}

def goose_run(ghome, n):
    cmd = ["goose", "run", os.path.join(SRC, "goose", "main.goose"), "--", str(n)]
    return cmd, {"GOOSE_HOME": ghome}

# ---------- phase runners ----------
def phase_cold(rounds=5):
    for rep in range(rounds):
        for lang in ["rust", "zig", "goose"]:
            d = os.path.join(WORK, f"cold_{lang}_{rep}")
            clean(d); os.makedirs(d, exist_ok=True)
            if lang == "rust":
                cmd, env = rust_build(d, release=False)
                p, w, c = run_cmd(cmd, env)
                extra = {"disk_bytes": dir_bytes(d),
                         "bin_bytes": file_size(os.path.join(d, "target/debug/l1kernel"))}
            elif lang == "zig":
                cmd, env = zig_build(d, os.path.join(d, "gcache"), "Debug")
                p, w, c = run_cmd(cmd, env)
                extra = {"disk_bytes": dir_bytes(d),
                         "bin_bytes": file_size(os.path.join(d, "l1kernel"))}
            else:
                gh = os.path.join(d, "gh")
                cmd, env = goose_build(gh, os.path.join(d, "exe"), debug=True)
                p, w, c = run_cmd(cmd, env)
                extra = {"disk_bytes": dir_bytes(d),
                         "bin_bytes": file_size(os.path.join(d, "exe"))}
            record("cold-dev", lang, rep, cmd, w, c, p, extra)
    # cold-opt
    for rep in range(rounds):
        for lang in ["rust", "zig", "goose"]:
            d = os.path.join(WORK, f"coldopt_{lang}_{rep}")
            clean(d); os.makedirs(d, exist_ok=True)
            if lang == "rust":
                cmd, env = rust_build(d, release=True)
                p, w, c = run_cmd(cmd, env)
                extra = {"disk_bytes": dir_bytes(d),
                         "bin_bytes": file_size(os.path.join(d, "target/release/l1kernel"))}
            elif lang == "zig":
                cmd, env = zig_build(d, os.path.join(d, "gcache"), "ReleaseFast")
                p, w, c = run_cmd(cmd, env)
                extra = {"disk_bytes": dir_bytes(d),
                         "bin_bytes": file_size(os.path.join(d, "l1kernel"))}
            else:
                gh = os.path.join(d, "gh")
                cmd, env = goose_build(gh, os.path.join(d, "exe"), debug=False)
                p, w, c = run_cmd(cmd, env)
                extra = {"disk_bytes": dir_bytes(d),
                         "bin_bytes": file_size(os.path.join(d, "exe"))}
            record("cold-opt", lang, rep, cmd, w, c, p, extra)

def phase_warm(reps=10):
    # persistent dirs
    for lang in ["rust", "zig", "goose"]:
        if lang == "rust":
            d = os.path.join(WORK, "warm_rust"); clean(d); os.makedirs(d, exist_ok=True)
            cmd, env = rust_build(d, release=True)
        elif lang == "zig":
            d = os.path.join(WORK, "warm_zig"); clean(d); os.makedirs(d, exist_ok=True)
            cmd, env = zig_build(os.path.join(d, "c"), os.path.join(d, "g"), "ReleaseFast")
        else:
            d = os.path.join(WORK, "warm_goose"); clean(d); os.makedirs(d, exist_ok=True)
            cmd, env = goose_build(os.path.join(d, "gh"), os.path.join(d, "exe"))
        # initial build
        p, w, c = run_cmd(cmd, env)
        record("warm-init", lang, 0, cmd, w, c, p)
        for rep in range(reps):
            p, w, c = run_cmd(cmd, env)
            record("warm-noop", lang, rep, cmd, w, c, p,
                   {"disk_bytes": dir_bytes(d)})

def edit_file(path, old, new):
    with open(path) as f:
        t = f.read()
    assert old in t, f"edit hook missing in {path}"
    with open(path, "w") as f:
        f.write(t.replace(old, new))

def restore_file(path, orig):
    with open(path, "w") as f:
        f.write(orig)

def phase_edit(reps=10):
    # Rust: edit kernel.rs EMA_DIVISOR 16->32
    prep = [
        ("rust", os.path.join(SRC, "rust", "src", "kernel.rs"), "EMA_DIVISOR: i64 = 16", "EMA_DIVISOR: i64 = 32"),
        ("zig", os.path.join(SRC, "zig", "kernel.zig"), "EMA_DIVISOR: i64 = 16", "EMA_DIVISOR: i64 = 32"),
        ("goose", os.path.join(SRC, "goose", "kernel.goose"), "EMA_DIVISOR: i64 = 16", "EMA_DIVISOR: i64 = 32"),
    ]
    for lang, path, old, new in prep:
        with open(path) as f:
            orig = f.read()
        if lang == "rust":
            d = os.path.join(WORK, "edit_rust"); clean(d); os.makedirs(d, exist_ok=True)
            cmd, env = rust_build(d, release=True)
            exe = os.path.join(d, "target/release/l1kernel")
        elif lang == "zig":
            d = os.path.join(WORK, "edit_zig"); clean(d); os.makedirs(d, exist_ok=True)
            cmd, env = zig_build(os.path.join(d, "c"), os.path.join(d, "g"), "ReleaseFast")
            exe = os.path.join(d, "c", "l1kernel")
        else:
            d = os.path.join(WORK, "edit_goose"); clean(d); os.makedirs(d, exist_ok=True)
            cmd, env = goose_build(os.path.join(d, "gh"), os.path.join(d, "exe"))
            exe = os.path.join(d, "exe")
        p0, w0, c0 = run_cmd(cmd, env)
        record("edit-init", lang, 0, cmd, w0, c0, p0)
        for rep in range(reps):
            edit_file(path, old, new)
            p, w, c = run_cmd(cmd, env)
            out = p.stdout or ""
            checksum = [l for l in out.splitlines() if l.startswith("checksum=")]
            record("warm-edit", lang, rep, cmd, w, c, p, {"checksum_line": checksum})
            restore_file(path, orig)
            p2, w2, c2 = run_cmd(cmd, env)
            record("warm-edit-restore", lang, rep, cmd, w2, c2, p2)

def phase_dep(reps=5):
    prep = [
        ("rust", os.path.join(SRC, "rust", "src", "kernel.rs"), "FNV_PRIME: u64 = 1099511628211", "FNV_PRIME: u64 = 1099511628281"),
        ("zig", os.path.join(SRC, "zig", "kernel.zig"), "FNV_PRIME: u64 = 1099511628211", "FNV_PRIME: u64 = 1099511628281"),
        ("goose", os.path.join(SRC, "goose", "kernel.goose"), "FNV_PRIME: u64 = 1099511628211", "FNV_PRIME: u64 = 1099511628281"),
    ]
    for lang, path, old, new in prep:
        with open(path) as f:
            orig = f.read()
        if lang == "rust":
            d = os.path.join(WORK, "dep_rust"); clean(d); os.makedirs(d, exist_ok=True)
            cmd, env = rust_build(d, release=True)
        elif lang == "zig":
            d = os.path.join(WORK, "dep_zig"); clean(d); os.makedirs(d, exist_ok=True)
            cmd, env = zig_build(os.path.join(d, "c"), os.path.join(d, "g"), "ReleaseFast")
        else:
            d = os.path.join(WORK, "dep_goose"); clean(d); os.makedirs(d, exist_ok=True)
            cmd, env = goose_build(os.path.join(d, "gh"), os.path.join(d, "exe"))
        p0, w0, c0 = run_cmd(cmd, env)
        record("dep-init", lang, 0, cmd, w0, c0, p0)
        for rep in range(reps):
            edit_file(path, old, new)
            p, w, c = run_cmd(cmd, env)
            record("warm-dep", lang, rep, cmd, w, c, p)
            restore_file(path, orig)
            p2, w2, c2 = run_cmd(cmd, env)
            record("warm-dep-restore", lang, rep, cmd, w2, c2, p2)

def phase_jit(reps_first=5, reps_warm=10, reps_edit=10):
    n = 1000000
    # first runs: fresh GOOSE_HOME each
    for rep in range(reps_first):
        d = os.path.join(WORK, f"jitfirst_{rep}"); clean(d); os.makedirs(d, exist_ok=True)
        cmd, env = goose_run(os.path.join(d, "gh"), n)
        p, w, c = run_cmd(cmd, env)
        record("goose-jit-first", "goose", rep, cmd, w, c, p, {"disk_bytes": dir_bytes(d)})
    # warm runs: shared GOOSE_HOME
    d = os.path.join(WORK, "jitwarm"); clean(d); os.makedirs(d, exist_ok=True)
    gh = os.path.join(d, "gh")
    cmd, env = goose_run(gh, n)
    p, w, c = run_cmd(cmd, env)
    record("goose-jit-warm-init", "goose", 0, cmd, w, c, p)
    for rep in range(reps_warm):
        p, w, c = run_cmd(cmd, env)
        record("goose-jit-warm", "goose", rep, cmd, w, c, p, {"disk_bytes": dir_bytes(d)})
    # edit runs: semantic edit of kernel.goose, then JIT run
    path = os.path.join(SRC, "goose", "kernel.goose")
    with open(path) as f:
        orig = f.read()
    for rep in range(reps_edit):
        edit_file(path, "EMA_DIVISOR: i64 = 16", "EMA_DIVISOR: i64 = 32")
        cmd, env = goose_run(gh, n)
        p, w, c = run_cmd(cmd, env)
        out = p.stdout or ""
        checksum = [l for l in out.splitlines() if l.startswith("checksum=")]
        record("goose-jit-edit", "goose", rep, cmd, w, c, p, {"checksum_line": checksum})
        restore_file(path, orig)
        p2, w2, c2 = run_cmd(cmd, env)
        record("goose-jit-edit-restore", "goose", rep, cmd, w2, c2, p2)

def phase_runtime(reps=5):
    bins = {}
    # build optimized binaries once
    d = os.path.join(WORK, "runtime"); clean(d); os.makedirs(d, exist_ok=True)
    cmd, env = rust_build(os.path.join(d, "rust"), release=True)
    p, w, c = run_cmd(cmd, env)
    bins["rust"] = os.path.join(d, "rust", "target", "release", "l1kernel")
    record("runtime-build", "rust", 0, cmd, w, c, p, {"bin_bytes": file_size(bins["rust"])})
    cmd, env = zig_build(os.path.join(d, "zig", "c"), os.path.join(d, "zig", "g"), "ReleaseFast")
    p, w, c = run_cmd(cmd, env)
    bins["zig"] = os.path.join(d, "zig", "c", "l1kernel")
    record("runtime-build", "zig", 0, cmd, w, c, p, {"bin_bytes": file_size(bins["zig"])})
    cmd, env = zig_build(os.path.join(d, "zigsafe", "c"), os.path.join(d, "zigsafe", "g"), "ReleaseSafe")
    p, w, c = run_cmd(cmd, env)
    bins["zigsafe"] = os.path.join(d, "zigsafe", "c", "l1kernel")
    record("runtime-build", "zigsafe", 0, cmd, w, c, p, {"bin_bytes": file_size(bins["zigsafe"])})
    cmd, env = goose_build(os.path.join(d, "gh"), os.path.join(d, "goose_exe"))
    p, w, c = run_cmd(cmd, env)
    bins["goose"] = os.path.join(d, "goose_exe")
    record("runtime-build", "goose", 0, cmd, w, c, p, {"bin_bytes": file_size(bins["goose"])})
    # stripped sizes
    for lang, b in list(bins.items()):
        if os.path.exists(b):
            sc = b + ".stripped"
            shutil.copy(b, sc)
            subprocess.run(["strip", sc])
            record("binary-size", lang, 0, [b], 0, 0,
                   subprocess.CompletedProcess([b], 0, "", ""), {"bin_bytes": file_size(b), "stripped_bytes": file_size(sc)})
    expected = {}
    for n in ["1000000", "10000000"]:
        with open(os.path.join(ROOT, "fixtures", f"expected_{n}.txt"), "rb") as f:
            expected[n] = hashlib.sha256(f.read()).hexdigest()
    for n, reps in [("1000000", reps), ("10000000", reps)]:
        for rep in range(reps):
            order = ["rust", "zig", "zigsafe", "goose"]
            random.Random(SEED + hash((n, rep)) % 10000).shuffle(order)
            for lang in order:
                if lang == "zigsafe":
                    b = bins["zigsafe"]; cmd = [b, n]
                elif lang == "goose":
                    b = bins["goose"]; cmd = [b, n]
                else:
                    b = bins[lang]; cmd = [b, n]
                if not os.path.exists(b):
                    record("runtime", lang, rep, cmd, 0, 0,
                           subprocess.CompletedProcess(cmd, -1, "", "missing binary"), {"n": n})
                    continue
                p, w, c = run_cmd(cmd)
                ok = hashlib.sha256((p.stdout or "").encode()).hexdigest() == expected[n]
                record("runtime", lang, rep, cmd, w, c, p,
                       {"n": n, "events_per_sec": round(int(n) / w, 1) if w > 0 else -1, "valid": ok})

def main():
    filters = sys.argv[1:]
    phases = []
    if not filters or "cold" in filters: phases.append(phase_cold)
    if not filters or "warm" in filters: phases.append(phase_warm)
    if not filters or "edit" in filters: phases.append(phase_edit)
    if not filters or "dep" in filters: phases.append(phase_dep)
    if not filters or "jit" in filters: phases.append(phase_jit)
    if not filters or "runtime" in filters: phases.append(phase_runtime)
    for ph in phases:
        print(f"=== {ph.__name__} ===", flush=True)
        ph()
    print("DONE")

if __name__ == "__main__":
    main()
