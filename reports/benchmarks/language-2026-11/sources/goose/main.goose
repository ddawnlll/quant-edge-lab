// L1 memory-only kernel driver (Goose). Usage: l1kernel <N> [--boundary]
import .kernel;
import os;
import std;

struct Ev { kind: u32, ts_off: i64, price: i64, qty: u64 }

fn report(count: u64, ema: i64, h: u64, funding: u64, state: u64, qty: u64, td: u64) {
    print("count=", count);
    print("ema=", ema);
    var b: u8[>..] = [];
    format_uint(b, h, 16, 16, '0');
    print("checksum=0x", b);
    print("funding=", funding);
    print("state=", state);
    print("qty=", qty);
    print("tdigest=", td);
}

fn main() {
    var n: u64 = 1000000;
    var boundary = false;
    let argv = args();
    if argv.len >= 2 {
        if argv[1] == "--boundary" {
            boundary = true;
        } else {
            let v, ok = parse_uint(argv[1]);
            if ok { n = v; }
        }
    }
    var state: u32 = 0;
    var ema: i64 = 0;
    var h: u64 = SEED;
    var funding: u64 = 0;
    var qty_total: u64 = 0;
    var trans: u64[>..] = [];
    for i in 144 { trans.push(0); }

    if boundary {
        var ev: Ev[>..] = [];
        ev.push(Ev { 0, 0, 100000000, 5 });
        ev.push(Ev { 11, 0, 100000000, 0 });
        ev.push(Ev { 11, 0, 100000001, 0 });
        ev.push(Ev { 1, 1000, -4000000000000000000, 9999 });
        ev.push(Ev { 2, 1000, 4000000000000000000, 1 });
        ev.push(Ev { 3, 2000, -4000000000000000000, 0 });
        ev.push(Ev { 4, 2000, 4000000000000000000, 9999 });
        ev.push(Ev { 5, 3000, 98765432, 1234 });
        ev.push(Ev { 6, 3000, 98765432, 1234 });
        ev.push(Ev { 7, 4000, -12345678, 9999 });
        ev.push(Ev { 8, 4000, 0, 0 });
        ev.push(Ev { 9, 5000, 77777777, 42 });
        ev.push(Ev { 10, 5000, 77777777, 42 });
        ev.push(Ev { 11, 6000, 1000, 1000 });
        ev.push(Ev { 0, 7000, 1000, 1000 });
        ev.push(Ev { 11, 7000, -1000, 0 });
        ev.push(Ev { 5, 8000, 2000000000, 9999 });
        ev.push(Ev { 6, 9000, -2000000000, 9999 });
        ev.push(Ev { 7, 10000, 3000000000, 7 });
        ev.push(Ev { 8, 10000, -3000000000, 7 });
        ev.push(Ev { 9, 11000, 123456789, 9999 });
        ev.push(Ev { 10, 12000, 123456789, 9999 });
        ev.push(Ev { 11, 12000, 99999999, 1 });
        ev.push(Ev { 1, 13000, 100000000, 5000 });
        var idx: i64 = 0;
        while idx < ev.len {
            let e = ev[idx];
            let ts = TS_BASE + e.ts_off;
            if idx == 0 {
                ema = e.price;
            } else {
                ema = ema_step(ema, e.price);
            }
            let cell = (state as i64) * 12 + (e.kind as i64);
            trans[cell] = trans[cell] + 1;
            state = next_state(state, e.kind);
            if e.kind == 11 { funding = funding + 1; }
            qty_total = qty_total + e.qty;
            h = mix(h, ts as u64);
            h = mix(h, e.price as u64);
            h = mix(h, e.qty);
            h = mix(h, e.kind as u64);
            h = mix(h, idx as u64);
            idx = idx + 1;
        }
        var td: u64 = 0;
        for i in 144 { td = td + trans[i] * ((i + 1) as u64); }
        report(ev.len as u64, ema, h, funding, state as u64, qty_total, td);
        return;
    }

    var rnd: u64 = SEED;
    var i: u64 = 0;
    while i < n {
        let s1, r1 = next_rand(rnd);
        let s2, r2 = next_rand(s1);
        let s3, r3 = next_rand(s2);
        let s4, r4 = next_rand(s3);
        rnd = s4;
        let kind = (r1 % 12) as u32;
        let ts = TS_BASE + ((r2 % 1000000) as i64) * 1000 + ((i % 3) as i64);
        let price = 100000000 + ((r3 % 2000000) as i64) - 1000000;
        let qty = r4 % 10000;
        if i == 0 {
            ema = price;
        } else {
            ema = ema_step(ema, price);
        }
        let cell = (state as i64) * 12 + (kind as i64);
        trans[cell] = trans[cell] + 1;
        state = next_state(state, kind);
        if kind == 11 { funding = funding + 1; }
        qty_total = qty_total + qty;
        h = mix(h, ts as u64);
        h = mix(h, price as u64);
        h = mix(h, qty);
        h = mix(h, kind as u64);
        h = mix(h, i);
        i = i + 1;
    }
    var td: u64 = 0;
    for i in 144 { td = td + trans[i] * ((i + 1) as u64); }
    report(n, ema, h, funding, state as u64, qty_total, td);
}
