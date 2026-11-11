// L1 shared kernel module (Goose): identical spec to kernel.rs / kernel.zig.
const SEED: u64 = 88172645463325252;
const FNV_PRIME: u64 = 1099511628211;
const EMA_DIVISOR: i64 = 16;
const TS_BASE: i64 = 1700000000000000;

fn next_rand(state: u64) -> u64, u64 {
    var x = state;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    return x, x * 2685821657736338717;
}

fn ema_step(ema: i64, price: i64) -> i64 {
    let diff = price - ema;
    let step = if diff >= 0 { (diff + 8) / EMA_DIVISOR } else { (diff - 8) / EMA_DIVISOR };
    return ema + step;
}

fn next_state(state: u32, kind: u32) -> u32 {
    return (state * 5 + kind + 1) % 12;
}

fn mix(h: u64, v: u64) -> u64 {
    return (h ^ v) * FNV_PRIME;
}
