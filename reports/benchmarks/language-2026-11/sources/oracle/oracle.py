#!/usr/bin/env python3
"""L1 reference oracle (bit-exact). Usage: oracle.py <N> | oracle.py --boundary"""
import sys

SEED = 88172645463325252
PRIME = 1099511628211
TS_BASE = 1700000000000000
M64 = (1 << 64) - 1


def mask(x):
    return x & M64


def bits(x):
    return x & M64  # two's complement low 64 bits


def nxt(s):
    x = s & M64
    x ^= (x >> 12) & M64
    x &= M64
    x ^= (x << 25) & M64
    x &= M64
    x ^= (x >> 27) & M64
    out = mask(x * 2685821657736338717)
    return x, out


def ema_step(ema, price):
    diff = price - ema
    # trunc-toward-zero with half-away rounding, matching Rust/Zig/Goose:
    if diff >= 0:
        step = (diff + 8) // 16
    else:
        step = -((-diff + 8) // 16)  # trunc toward zero of (diff-8)/16
    return ema + step


def mix(h, v):
    return mask((h ^ v) * PRIME)


def run(n):
    state = 0
    rnd = SEED
    ema = 0
    h = SEED
    funding = 0
    qty_total = 0
    trans = [0] * 144
    for i in range(n):
        rnd, r1 = nxt(rnd)
        rnd, r2 = nxt(rnd)
        rnd, r3 = nxt(rnd)
        rnd, r4 = nxt(rnd)
        kind = r1 % 12
        ts = TS_BASE + (r2 % 1000000) * 1000 + (i % 3)
        price = 100000000 + (r3 % 2000000) - 1000000
        qty = r4 % 10000
        ema = price if i == 0 else ema_step(ema, price)
        trans[state * 12 + kind] = mask(trans[state * 12 + kind] + 1)
        state = (state * 5 + kind + 1) % 12
        if kind == 11:
            funding = mask(funding + 1)
        qty_total = mask(qty_total + qty)
        h = mix(h, bits(ts))
        h = mix(h, bits(price))
        h = mix(h, qty)
        h = mix(h, kind)
        h = mix(h, i)
    td = 0
    for idx, v in enumerate(trans):
        td = mask(td + mask(v * (idx + 1)))
    return n, ema, h, funding, state, qty_total, td


BOUNDARY = [
    (0, 0, 100000000, 5), (11, 0, 100000000, 0), (11, 0, 100000001, 0),
    (1, 1000, -4000000000000000000, 9999), (2, 1000, 4000000000000000000, 1),
    (3, 2000, -4000000000000000000, 0), (4, 2000, 4000000000000000000, 9999),
    (5, 3000, 98765432, 1234), (6, 3000, 98765432, 1234),
    (7, 4000, -12345678, 9999), (8, 4000, 0, 0),
    (9, 5000, 77777777, 42), (10, 5000, 77777777, 42),
    (11, 6000, 1000, 1000), (0, 7000, 1000, 1000), (11, 7000, -1000, 0),
    (5, 8000, 2000000000, 9999), (6, 9000, -2000000000, 9999),
    (7, 10000, 3000000000, 7), (8, 10000, -3000000000, 7),
    (9, 11000, 123456789, 9999), (10, 12000, 123456789, 9999),
    (11, 12000, 99999999, 1), (1, 13000, 100000000, 5000),
]


def run_boundary():
    state = 0
    ema = 0
    h = SEED
    funding = 0
    qty_total = 0
    trans = [0] * 144
    for i, (kind, ts_off, price, qty) in enumerate(BOUNDARY):
        ts = TS_BASE + ts_off
        ema = price if i == 0 else ema_step(ema, price)
        trans[state * 12 + kind] = mask(trans[state * 12 + kind] + 1)
        state = (state * 5 + kind + 1) % 12
        if kind == 11:
            funding = mask(funding + 1)
        qty_total = mask(qty_total + qty)
        h = mix(h, bits(ts))
        h = mix(h, bits(price))
        h = mix(h, qty)
        h = mix(h, kind)
        h = mix(h, i)
    td = 0
    for idx, v in enumerate(trans):
        td = mask(td + mask(v * (idx + 1)))
    return len(BOUNDARY), ema, h, funding, state, qty_total, td


def report(t):
    n, ema, h, funding, state, qty, td = t
    print(f"count={n}")
    print(f"ema={ema}")
    print(f"checksum=0x{h:016x}")
    print(f"funding={funding}")
    print(f"state={state}")
    print(f"qty={qty}")
    print(f"tdigest={td}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--boundary":
        report(run_boundary())
    else:
        n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000000
        report(run(n))
