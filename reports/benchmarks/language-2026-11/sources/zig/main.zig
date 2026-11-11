// L1 memory-only kernel driver (Zig). Usage: l1kernel <N> [--boundary]
const std = @import("std");
const k = @import("kernel.zig");

const Ev = struct { kind: u32, ts_off: i64, price: i64, qty: u64 };

const boundary_events: [24]Ev = .{
    .{ .kind = 0, .ts_off = 0, .price = 100000000, .qty = 5 },
    .{ .kind = 11, .ts_off = 0, .price = 100000000, .qty = 0 },
    .{ .kind = 11, .ts_off = 0, .price = 100000001, .qty = 0 },
    .{ .kind = 1, .ts_off = 1000, .price = -4000000000000000000, .qty = 9999 },
    .{ .kind = 2, .ts_off = 1000, .price = 4000000000000000000, .qty = 1 },
    .{ .kind = 3, .ts_off = 2000, .price = -4000000000000000000, .qty = 0 },
    .{ .kind = 4, .ts_off = 2000, .price = 4000000000000000000, .qty = 9999 },
    .{ .kind = 5, .ts_off = 3000, .price = 98765432, .qty = 1234 },
    .{ .kind = 6, .ts_off = 3000, .price = 98765432, .qty = 1234 },
    .{ .kind = 7, .ts_off = 4000, .price = -12345678, .qty = 9999 },
    .{ .kind = 8, .ts_off = 4000, .price = 0, .qty = 0 },
    .{ .kind = 9, .ts_off = 5000, .price = 77777777, .qty = 42 },
    .{ .kind = 10, .ts_off = 5000, .price = 77777777, .qty = 42 },
    .{ .kind = 11, .ts_off = 6000, .price = 1000, .qty = 1000 },
    .{ .kind = 0, .ts_off = 7000, .price = 1000, .qty = 1000 },
    .{ .kind = 11, .ts_off = 7000, .price = -1000, .qty = 0 },
    .{ .kind = 5, .ts_off = 8000, .price = 2000000000, .qty = 9999 },
    .{ .kind = 6, .ts_off = 9000, .price = -2000000000, .qty = 9999 },
    .{ .kind = 7, .ts_off = 10000, .price = 3000000000, .qty = 7 },
    .{ .kind = 8, .ts_off = 10000, .price = -3000000000, .qty = 7 },
    .{ .kind = 9, .ts_off = 11000, .price = 123456789, .qty = 9999 },
    .{ .kind = 10, .ts_off = 12000, .price = 123456789, .qty = 9999 },
    .{ .kind = 11, .ts_off = 12000, .price = 99999999, .qty = 1 },
    .{ .kind = 1, .ts_off = 13000, .price = 100000000, .qty = 5000 },
};

fn writeOut(comptime fmt: []const u8, args: anytype) void {
    var buf: [256]u8 = undefined;
    const s = std.fmt.bufPrint(&buf, fmt, args) catch return;
    std.debug.print("{s}", .{s});
}

fn report(count: u64, ema: i64, h: u64, funding: u64, state: u64, qty: u64, td: u64) void {
    writeOut("count={d}\n", .{count});
    writeOut("ema={d}\n", .{ema});
    writeOut("checksum=0x{x:0>16}\n", .{h});
    writeOut("funding={d}\n", .{funding});
    writeOut("state={d}\n", .{state});
    writeOut("qty={d}\n", .{qty});
    writeOut("tdigest={d}\n", .{td});
}

fn step(state: *u32, trans: *[144]u64, funding: *u64, qty_total: *u64, h: *u64, ema: *i64, first: bool, kind: u32, ts: i64, price: i64, qty: u64, seq: u64) void {
    if (first) {
        ema.* = price;
    } else {
        ema.* = k.emaStep(ema.*, price);
    }
    const cur: usize = @intCast(state.*);
    const kk: usize = @intCast(kind);
    trans[cur * 12 + kk] = trans[cur * 12 + kk] +% 1;
    state.* = k.nextState(state.*, kind);
    if (kind == 11) funding.* = funding.* +% 1;
    qty_total.* = qty_total.* +% qty;
    h.* = k.mix(h.*, @bitCast(ts));
    h.* = k.mix(h.*, @bitCast(price));
    h.* = k.mix(h.*, qty);
    h.* = k.mix(h.*, kind);
    h.* = k.mix(h.*, seq);
}

pub fn main(init: std.process.Init.Minimal) !void {
    var n: u64 = 1000000;
    var boundary = false;
    for (init.args.vector) |a| {
        const s: []const u8 = std.mem.span(a);
        if (std.mem.eql(u8, s, "--boundary")) {
            boundary = true;
        } else if (std.fmt.parseInt(u64, s, 10) catch null) |v| {
            n = v;
        } else {}
    }
    if (boundary) {
        var state: u32 = 0;
        var ema: i64 = 0;
        var h: u64 = k.SEED;
        var funding: u64 = 0;
        var qty_total: u64 = 0;
        var trans: [144]u64 = undefined;
        @memset(&trans, 0);
        for (boundary_events, 0..) |e, i| {
            step(&state, &trans, &funding, &qty_total, &h, &ema, i == 0, e.kind, k.TS_BASE + e.ts_off, e.price, e.qty, @intCast(i));
        }
        var td: u64 = 0;
        for (trans, 0..) |v, idx| td = td +% v *% @as(u64, @intCast(idx + 1));
        report(boundary_events.len, ema, h, funding, state, qty_total, td);
        return;
    }
    var state: u32 = 0;
    var rnd: u64 = k.SEED;
    var ema: i64 = 0;
    var h: u64 = k.SEED;
    var funding: u64 = 0;
    var qty_total: u64 = 0;
    var trans: [144]u64 = undefined;
        @memset(&trans, 0);
    var i: u64 = 0;
    while (i < n) : (i += 1) {
        const a1 = k.nextRand(rnd);
        const a2 = k.nextRand(a1.s);
        const a3 = k.nextRand(a2.s);
        const a4 = k.nextRand(a3.s);
        rnd = a4.s;
        const kind: u32 = @intCast(a1.out % 12);
        const ts = k.TS_BASE + @as(i64, @intCast(a2.out % 1000000)) * 1000 + @as(i64, @intCast(i % 3));
        const price = 100000000 + @as(i64, @intCast(a3.out % 2000000)) - 1000000;
        const qty = a4.out % 10000;
        step(&state, &trans, &funding, &qty_total, &h, &ema, i == 0, kind, ts, price, qty, i);
    }
    var td: u64 = 0;
    for (trans, 0..) |v, idx| td = td +% v *% @as(u64, @intCast(idx + 1));
    report(n, ema, h, funding, state, qty_total, td);
}
