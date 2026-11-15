#!/usr/bin/env python3
"""Checks of host/make-edid: structure, timings, limits.  python3 tests/test_make_edid.py"""
import importlib.machinery
import importlib.util
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("make_edid", os.path.join(HERE, "..", "host", "make-edid"))
spec = importlib.util.spec_from_loader("make_edid", loader)
me = importlib.util.module_from_spec(spec)
loader.exec_module(me)

FAILED = []


def check(label, got, want):
    if got != want:
        FAILED.append(label)
        print(f"FAIL {label}\n     got  {got!r}\n     want {want!r}")
    else:
        print(f"ok   {label}")


def timings(edid):
    out = []
    for k in range(4):
        d = edid[54 + 18 * k:72 + 18 * k]
        pclk = d[0] | d[1] << 8
        if pclk:
            ha = d[2] | (d[4] >> 4) << 8
            hb = d[3] | (d[4] & 0xF) << 8
            va = d[5] | (d[7] >> 4) << 8
            vb = d[6] | (d[7] & 0xF) << 8
            out.append((ha, va, round(pclk * 1e4 / ((ha + hb) * (va + vb)), 1), pclk))
    return out


e = me.build(2400, 1600, 120.0, [60.0], "Virtual", (305, 203))
check("128 bytes", len(e), 128)
check("header", e[:8], me.HEADER)
check("checksum", sum(e) & 0xFF, 0)
check("EDID 1.4", (e[18], e[19]), (1, 4))
check("no extension", e[126], 0)
t = timings(e)
check("preferred mode first", t[0][:3], (2400, 1600, 120.0))
check("60 Hz extra rate", t[1][:3], (2400, 1600, 60.0))
check("same pixel clock for both rates", t[0][3], t[1][3])
check("name descriptor", "name: Virtual" in me.decode(e), True)
check("range descriptor", "range: 60-120 Hz" in me.decode(e), True)

e = me.build(1920, 1080, 60.0, [], "TV", (480, 270))
t = timings(e)
check("single 60 Hz mode", [x[:3] for x in t], [(1920, 1080, 60.0)])
check("checksum 1080p", sum(e) & 0xFF, 0)

e = me.build(2532, 1170, 60.0, [], "Phone", (65, 30))
check("odd phone resolution", timings(e)[0][:3], (2532, 1170, 60.0))

try:
    me.build(3840, 2160, 120.0, [60.0], "TV", (600, 340))
    check("4K 120 Hz refused", "built", "ValueError")
except ValueError as err:
    check("4K 120 Hz refused", "655.35" in str(err), True)

check("4K 60 Hz fits", timings(me.build(3840, 2160, 60.0, [], "TV", (600, 340)))[0][:3], (3840, 2160, 60.0))

for bad in (b"", b"\x00" * 128, me.HEADER + b"\x00" * 120):
    try:
        me.decode(bad)
        check(f"decode rejects {bad[:4]!r}...", "accepted", "ValueError")
    except ValueError:
        check(f"decode rejects {bad[:4]!r}...", True, True)

for w, h, r in ((1920, 1080, 240), (1600, 2560, 120), (1920, 1200, 240)):  # the last one only fits in CVT-RB v2
    try:
        e = me.build(w, h, float(r), [60.0], "Fast", (500, 300))
        check(f"{w}x{h}@{r}: built, checksum ok", sum(e) & 0xFF, 0)
        check(f"{w}x{h}@{r}: preferred rate", timings(e)[0][:3], (w, h, float(r)))
    except ValueError as err:
        check(f"{w}x{h}@{r}: built", str(err), "no error")
check("1920x1200@240 uses reduced blanking v2", me.timing(1920, 1200, 240.0)["h_blank"], 80)
check("2400x1600@120 keeps v1 (proven on real hardware)", me.timing(2400, 1600, 120.0)["h_blank"], 160)
try:
    me.build(2560, 1440, 165.0, [60.0], "TooFast", (600, 340))
    check("2560x1440@165 over the limit even in v2", "built", "ValueError")
except ValueError:
    check("2560x1440@165 over the limit even in v2", True, True)
try:
    me.build(5120, 1440, 60.0, [], "Wide", (1200, 340))
    check("5120 wide refused", "built", "ValueError")
except ValueError as err:
    check("5120 wide refused with a clear message", "4095" in str(err), True)

check("decode: horizontal rate above 255 kHz", "291-292 kHz" in me.decode(me.build(1920, 1080, 240.0, [60.0], "F", (500, 300))), True)

print(f"\n{len(FAILED)} failure(s)")
sys.exit(1 if FAILED else 0)
