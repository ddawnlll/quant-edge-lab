// L1 shared kernel module: PRNG, EMA step, state machine, checksum step.
// All integer semantics are explicitly wrapping or truncation-toward-zero so
// that Rust, Zig, Goose and the Python oracle agree bit-for-bit.
// Edit hooks for benchmark matrix:
//   - Warm edit:   EMA_DIVISOR (16 -> 32)
//   - Warm dep:    FNV_PRIME (shared constant consumed via kernel module)
pub const SEED: u64 = 88172645463325252;
pub const FNV_PRIME: u64 = 1099511628211;
pub const EMA_DIVISOR: i64 = 16;
pub const TS_BASE: i64 = 1700000000000000;
pub const STATES: usize = 12;

// xorshift64*: advances state, returns (new_state, output).
pub fn next_rand(state: u64) -> (u64, u64) {
    let mut x = state;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    let out = x.wrapping_mul(2685821657736338717);
    (x, out)
}

// EMA step with round-half-away-from-zero by EMA_DIVISOR.
// diff = price - ema; if diff >= 0 step=(diff+8)/D else step=(diff-8)/D,
// integer division truncates toward zero (Rust/Zig/C/Goose agree).
// Price magnitude is bounded to +/-4e18 so diff never overflows i64.
pub fn ema_step(ema: i64, price: i64) -> i64 {
    let diff = price.wrapping_sub(ema);
    let step = if diff >= 0 {
        (diff + 8) / EMA_DIVISOR
    } else {
        (diff - 8) / EMA_DIVISOR
    };
    ema.wrapping_add(step)
}

// 12-variant state machine transition.
pub fn next_state(state: u32, kind: u32) -> u32 {
    ((state * 5 + kind + 1) % 12) as u32
}

// FNV-style checksum step, wrapping u64 arithmetic.
pub fn mix(h: u64, v: u64) -> u64 {
    (h ^ v).wrapping_mul(FNV_PRIME)
}
