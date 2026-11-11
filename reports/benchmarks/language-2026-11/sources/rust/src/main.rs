// L1 memory-only kernel driver (Rust).
// Usage: l1kernel <N> [--boundary]
// Default N = 1000000. --boundary runs the fixed 24-event edge suite.
// Output is canonical key=value lines, identical across languages.
mod kernel;
use kernel::{mix, next_rand, next_state, ema_step, SEED, TS_BASE};

fn run_streaming(n: u64) -> (i64, u64, u64, u64, u64, u64) {
    let mut state: u32 = 0;
    let mut rand_state: u64 = SEED;
    let mut ema: i64 = 0;
    let mut h: u64 = SEED;
    let mut funding: u64 = 0;
    let mut qty_total: u64 = 0;
    let mut trans = [0u64; 144];
    for i in 0..n {
        let (ns1, r1) = next_rand(rand_state);
        let (ns2, r2) = next_rand(ns1);
        let (ns3, r3) = next_rand(ns2);
        let (ns4, r4) = next_rand(ns3);
        rand_state = ns4;
        let kind = (r1 % 12) as u32;
        let ts = TS_BASE + ((r2 % 1000000) as i64) * 1000 + ((i % 3) as i64);
        let price = 100000000 + ((r3 % 2000000) as i64) - 1000000;
        let qty = r4 % 10000;
        if i == 0 {
            ema = price;
        } else {
            ema = ema_step(ema, price);
        }
        let cur = state as usize;
        let k = kind as usize;
        trans[cur * 12 + k] = trans[cur * 12 + k].wrapping_add(1);
        state = next_state(state, kind);
        if kind == 11 {
            funding = funding.wrapping_add(1);
        }
        qty_total = qty_total.wrapping_add(qty);
        h = mix(h, ts as u64);
        h = mix(h, price as u64);
        h = mix(h, qty);
        h = mix(h, kind as u64);
        h = mix(h, i);
    }
    let mut tdigest: u64 = 0;
    for idx in 0..144 {
        tdigest = tdigest.wrapping_add(trans[idx].wrapping_mul((idx as u64).wrapping_add(1)));
    }
    (ema, h, funding, state as u64, qty_total, tdigest)
}

fn report(n: u64, ema: i64, h: u64, funding: u64, state: u64, qty: u64, td: u64) {
    println!("count={}", n);
    println!("ema={}", ema);
    println!("checksum=0x{:016x}", h);
    println!("funding={}", funding);
    println!("state={}", state);
    println!("qty={}", qty);
    println!("tdigest={}", td);
}

fn run_boundary() {
    // Fixed edge suite: duplicate timestamps, funding bursts (kind 11),
    // zero quantities, extreme prices within +/-4e18, seq ties.
    // Columns: (kind, ts_offset, price, qty)
    let ev: [(u32, i64, i64, u64); 24] = [
        (0, 0, 100000000, 5),
        (11, 0, 100000000, 0),   // funding + zero qty + dup ts
        (11, 0, 100000001, 0),   // second funding, same ts
        (1, 1000, -4000000000000000000, 9999),
        (2, 1000, 4000000000000000000, 1),  // extreme swing, EMA stress
        (3, 2000, -4000000000000000000, 0),
        (4, 2000, 4000000000000000000, 9999),
        (5, 3000, 98765432, 1234),
        (6, 3000, 98765432, 1234),  // full duplicate of previous
        (7, 4000, -12345678, 9999),
        (8, 4000, 0, 0),
        (9, 5000, 77777777, 42),
        (10, 5000, 77777777, 42),
        (11, 6000, 1000, 1000),   // funding epoch attribution
        (0, 7000, 1000, 1000),
        (11, 7000, -1000, 0),
        (5, 8000, 2000000000, 9999),
        (6, 9000, -2000000000, 9999),
        (7, 10000, 3000000000, 7),
        (8, 10000, -3000000000, 7),
        (9, 11000, 123456789, 9999),
        (10, 12000, 123456789, 9999),
        (11, 12000, 99999999, 1),
        (1, 13000, 100000000, 5000),
    ];
    let mut state: u32 = 0;
    let mut ema: i64 = 0;
    let mut h: u64 = SEED;
    let mut funding: u64 = 0;
    let mut qty_total: u64 = 0;
    let mut trans = [0u64; 144];
    for (i, (kind, t_off, price, qty)) in ev.iter().enumerate() {
        let ts = TS_BASE + t_off;
        if i == 0 {
            ema = *price;
        } else {
            ema = ema_step(ema, *price);
        }
        let cur = state as usize;
        let k = *kind as usize;
        trans[cur * 12 + k] = trans[cur * 12 + k].wrapping_add(1);
        state = next_state(state, *kind);
        if *kind == 11 {
            funding = funding.wrapping_add(1);
        }
        qty_total = qty_total.wrapping_add(*qty);
        h = mix(h, ts as u64);
        h = mix(h, *price as u64);
        h = mix(h, *qty);
        h = mix(h, *kind as u64);
        h = mix(h, i as u64);
    }
    let mut tdigest: u64 = 0;
    for idx in 0..144 {
        tdigest = tdigest.wrapping_add(trans[idx].wrapping_mul((idx as u64).wrapping_add(1)));
    }
    report(ev.len() as u64, ema, h, funding, state as u64, qty_total, tdigest);
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "--boundary") {
        run_boundary();
        return;
    }
    let n: u64 = if args.len() > 1 {
        args[1].parse().unwrap_or(1000000)
    } else {
        1000000
    };
    let (ema, h, funding, state, qty, td) = run_streaming(n);
    report(n, ema, h, funding, state, qty, td);
}
