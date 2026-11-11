# QEL Language Benchmark Methodology v1.1 (2026-11-11)

Benchmark, not migration. No changes to production QEL code. All work under
scratch `~/quant/bench-lang-2026-11/` (outside tracked source).

## Environment (see ENVIRONMENT.json)
Debian 13, kernel 6.12.115+deb13.1, 6 vCPU Xeon E5-2680 v2, 12 GiB RAM,
160 GB SSD. cgroup cpu.max/memory.max absent (no limits). Load recorded per
trial; trials run with other compilation active are labeled SOAK-CONTENDED.
The 7-day QEL soak (soak.sh since 2026-11-08) is never stopped.

## Toolchain pins (see toolchain-lock.json)
rustc 1.99.1 stable, cargo 1.99.1, rustflags `-C link-arg=-fuse-ld=lld`
(global config). Zig 0.17.1 stable binary at
`/home/quant/local/opt/zig-0.17.1/zig`. Goose a12523f23e545a6af350562804a0c8b201864946
(binary runtime d521138194f1), AOT via `--cc clang` 19.1.7, default cc gcc 14.2.0,
cmake 3.31.6. Goose bootstrap (prebuilt binary) NOT counted: UNMEASURED cost,
binary provided. QEL commit 3678c6a (ddawnlll/quant-edge-lab).

## L1 kernel spec (language-only, no deps, no FFI)
Deterministic in-program generator (memory-only: no I/O). xorshift64*
seed 88172645463325252. Per event: kind=r1%12, ts=1700000000000000+(r2%1e6)*1000+(i%3)
(duplicates/ties), price=1e8+(r3%2e6)-1e6 fixed point, qty=r4%10000.
State machine 12 states, next=(state*5+kind+1)%12, 12x12 transition counts;
kind==11 counts as funding. EMA step: diff=price-ema, step=(diff+8)/16 if
diff>=0 else (diff-8)/16 (trunc toward zero, half-away), divisor 16.
Checksum: FNV-style wrapping u64 h^=v; h*=1099511628211 over ts,price,qty,kind,seq.
Output canonical key=value: count,ema,checksum,funding,state,qty,tdigest.
Boundary suite (--boundary): 24 fixed events (dup ts, funding bursts, zero qty,
price extremes to +/-4e18, EMA stress swings).
Python oracle (sources/oracle/oracle.py) is the reference. Parity = byte-identical
output at N=1000, boundary, 1M, 10M across Rust/Zig/Goose-AOT/Goose-JIT.

## Build matrix (serial; language order randomized per round)
- Rust dev: `cargo build` (debug); rust release: `cargo build --release`; check: `cargo check`.
- Rust isolated small kernel via `cargo build --manifest-path sources/rust/Cargo.toml`
  with benchmark-owned CARGO_TARGET_DIR per cold trial.
- Zig: `zig build-exe main.zig -O Debug|ReleaseFast|ReleaseSafe
  --cache-dir <unique> --global-cache-dir <unique>`.
- Goose AOT: `goose build --cc clang [--debug] -o exe main.goose` with
  benchmark-owned GOOSE_HOME. Goose JIT: `goose run main.goose N` (TinyCC
  in-process); fresh GOOSE_HOME first-run, warm rerun, edited rerun.
- Phases: cold-dev (fresh caches, debug), cold-opt (fresh caches, optimized),
  warm-noop, warm-edit (EMA_DIVISOR 16->32, verify hash changes, restore),
  warm-dep (FNV_PRIME changed in shared module, verify, restore),
  runtime (optimized binary, 1M and 10M, output validated vs oracle),
  goose-jit-first / goose-jit-warm / goose-jit-edit.
- Trials per phase: cold 5, warm 10, jit-first 5, jit-warm 10, jit-edit 10,
  runtime 5 per N per language.

## Metrics per trial
wall_s (perf_counter), CPU seconds (resource.getrusage children or /usr/bin/time -v),
peak RSS (getrusage ru_maxrss of wait4 or /usr/bin/time -v Maximum resident),
exit status, stdout sha256, disk allocated blocks (du --apparent-size absent;
use `du -sh` + block count), binary size (stripped via `strip` copy and `size`),
ldd dynamic deps. Per trial one JSON line appended to raw/trials.jsonl and a
summary row in results.csv. Median/p90/min/max computed in analysis.

## Safety matching
Rust debug vs Zig Debug vs Goose --debug; Rust release vs Zig ReleaseFast vs
Zig ReleaseSafe vs Goose optimized AOT. Goose JIT excluded from optimized
runtime ranking. Safety differences noted in VERDICT.

## L2
Only if L1 parity proven. Single-symbol taker-replay challenger; requires
native conformance tests before any parity claim. Default: UNMEASURED until L1 done.

## L3
Existing native/runner (nautilus git rev 27a8e54) and policy/rust measured
separately; full Nautilus graph not compared to toy standalone files.
Rust profile control experiments isolated in scratch CARGO_TARGET_DIRs.

## Fairness
Same input bytes (spec), no OOS data, no production changes, all failures
recorded, JIT/AOT hash mismatches are correctness failures not speed wins.
