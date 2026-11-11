// L1 shared kernel module (Zig): identical spec to Rust kernel.rs.
pub const SEED: u64 = 88172645463325252;
pub const FNV_PRIME: u64 = 1099511628211;
pub const EMA_DIVISOR: i64 = 16;
pub const TS_BASE: i64 = 1700000000000000;

pub const RandOut = struct { s: u64, out: u64 };

pub fn nextRand(state: u64) RandOut {
    var x = state;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    const out = x *% 2685821657736338717;
    return .{ .s = x, .out = out };
}

pub fn emaStep(ema: i64, price: i64) i64 {
    const diff = price -% ema;
    const step = if (diff >= 0) @divTrunc(diff + 8, EMA_DIVISOR) else @divTrunc(diff - 8, EMA_DIVISOR);
    return ema +% step;
}

pub fn nextState(state: u32, kind: u32) u32 {
    return (state * 5 + kind + 1) % 12;
}

pub fn mix(h: u64, v: u64) u64 {
    return (h ^ v) *% FNV_PRIME;
}
