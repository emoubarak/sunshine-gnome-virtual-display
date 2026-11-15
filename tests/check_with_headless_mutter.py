"""Checks fit_layout against a real Mutter, in a private session that never touches your desktop.

    dbus-run-session -- bash -c 'mutter --headless --virtual-monitor 1920x1080 \
        --virtual-monitor 1920x1080 --virtual-monitor 2400x1600 & sleep 4; \
        python3 tests/check_with_headless_mutter.py; kill %1'

Raw layouts that break Mutter's rules must be refused, the same layouts after
fit_layout accepted (ApplyMonitorsConfig method 0: verify only).
"""
import importlib.machinery, importlib.util, os, sys, time, tempfile
sys.dont_write_bytecode = True
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "host", "gnome-virtual-display")
L = importlib.machinery.SourceFileLoader("gvd", SCRIPT)
g = importlib.util.module_from_spec(importlib.util.spec_from_loader("gvd", L)); L.exec_module(g)
d = tempfile.mkdtemp()
for n, b in (("STATE_DIR", ""), ("MODE_FILE", "mode"), ("LOCK_FILE", "lock"), ("SESSION_FILE", "session"),
             ("MOUSE_FILE", "mouse"), ("PREFS_FILE", "prefs.json"), ("XML_WRITTEN", "h")):
    setattr(g, n, os.path.join(d, b) if b else d)
g.MONITORS_XML = os.path.join(d, "monitors.xml")
for _ in range(50):
    try:
        serial, mons, logical = g.state(); break
    except BaseException:
        time.sleep(0.2)
names = [m[0][0] for m in mons]
print("Mutter monitors:", [(m[0][0], [(x[1], x[2]) for x in m[1]][:1]) for m in mons])
virt = next(m for m in mons if any(x[1] == 2400 for x in m[1]))[0][0]
reals = [n for n in names if n != virt]
g.configure(connector=virt, edid="/nonexistent", sunshine_log=os.path.join(d, "x.log"))
g.helper = g.gsettings = g.restart_sunshine = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no"))
from gi.repository import GLib
def mode_id(conn, w):
    m = next(m for m in g.state()[1] if m[0][0] == conn)
    return next(x[0] for x in m[1] if x[1] == w)
FAILED = []


def verify(label, layout, want="ACCEPTED"):
    serial = g.state()[0]
    arg = GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (serial, 0, [tuple(x) for x in layout], {}))
    try:
        g.mutter_call("ApplyMonitorsConfig", arg); res = "ACCEPTED"
    except BaseException as e:
        res = "REFUSED: " + str(getattr(e, "message", e)).split(":")[-1].strip()
    ok = res.startswith(want)
    if not ok:
        FAILED.append(label)
    print(f"{'ok ' if ok else 'KO '} {label:62s} {res}")
r0, r1 = reals[0], reals[1]
m0, m1, mv = mode_id(r0, 1920), mode_id(r1, 1920), mode_id(virt, 2400)
# a) landscape 1920x1080 + virtual 2400x1600 at 1.25, centred (raw goes above 0)
raw = [(0, 0, 1.0, 0, False, [(r0, m0, {})]), (1920, -100, 1.25, 0, True, [(virt, mv, {})])]
verify("a) landscape + taller virtual, raw (y=-100)", raw, want="REFUSED")
verify("a) landscape + taller virtual, after fit_layout", g.fit_layout(raw))
# b) three monitors extend
raw3 = [(0, 0, 1.0, 0, False, [(r0, m0, {})]), (1920, 0, 1.0, 0, False, [(r1, m1, {})]),
        (3840, -100, 1.25, 0, True, [(virt, mv, {})])]
verify("b) 3 monitors + virtual, after fit_layout", g.fit_layout(raw3))
# c) middle monitor unplugged: gap
gap = [(0, 0, 1.0, 0, True, [(r0, m0, {})]), (3840, 0, 1.25, 0, False, [(virt, mv, {})])]
verify("c) gap after an unplugged monitor, raw", gap, want="REFUSED")
verify("c) gap, after fit_layout", g.fit_layout(gap))
# d) rotation makes a monitor overlap its neighbour
rot = [(0, 0, 1.0, 1, True, [(r0, m0, {})]), (1080, 0, 1.0, 0, False, [(r1, m1, {})])]
rot_bad = [(0, 0, 1.0, 0, True, [(r0, m0, {})]), (1080, 0, 1.0, 0, False, [(r1, m1, {})])]
verify("d) overlap (1920 wide monitor, neighbour at x=1080), raw", rot_bad, want="REFUSED")
verify("d) overlap, after fit_layout", g.fit_layout(rot_bad))
verify("d) portrait monitor + neighbour (valid), after fit_layout", g.fit_layout(rot))
# e) valid L layout untouched and accepted
lay = [(0, 0, 1.0, 0, True, [(r0, m0, {})]), (0, 1080, 1.0, 0, False, [(r1, m1, {})]),
       (1920, 0, 1.25, 0, False, [(virt, mv, {})])]
verify("e) L layout (valid), after fit_layout (unchanged: %s)" % (g.fit_layout(lay) == [tuple(x) for x in lay]), g.fit_layout(lay))
# f) the tool's own extend layout from Mutter's real state
serial, mons, logical = g.state()
g.load_prefs = lambda: {"virtual": {"scale": 1.25, "transform": 0, "x": None, "y": None}, "reals": {}}
built = g.build_layout(mons, [(0, 0, 1.0, 0, True, [m[0] for m in mons if m[0][0] == r0])])
verify("f) build_layout from the real state, through fit_layout", g.fit_layout(built))

print(f"\n{len(FAILED)} failure(s)")
sys.exit(1 if FAILED else 0)
