#!/usr/bin/env python3
"""Tests for the layout logic of gnome-virtual-display, without any hardware.

They simulate what Mutter returns (monitors + logical) and check what the tool
derives from it: never a mirror, physical monitor never changed, scale and
rotation of the virtual monitor kept, a way back to base from every mode.

    python3 tests/test_layout.py
"""
import copy
import importlib.machinery
import importlib.util
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "host", "gnome-virtual-display")
sys.dont_write_bytecode = True  # no __pycache__ next to the script
loader = importlib.machinery.SourceFileLoader("gvd", SCRIPT)
spec = importlib.util.spec_from_loader("gvd", loader)
gvd = importlib.util.module_from_spec(spec)
loader.exec_module(gvd)


def _forbidden(*a, **k):
    raise RuntimeError("test: call to the real Mutter, helper or system forbidden")


def isolate(module):
    """Points every state path of a loaded copy at a fresh temporary directory:
    a test must never touch the real mode, prefs, session, lock or monitors.xml
    (running the suite during a stream would otherwise end it)."""
    d = tempfile.mkdtemp()
    module.STATE_DIR = d
    for name, base in (("MODE_FILE", "mode"), ("LOCK_FILE", "lock"), ("SESSION_FILE", "session"),
                       ("MOUSE_FILE", "mouse"), ("PREFS_FILE", "prefs.json"),
                       ("XML_WRITTEN", "monitors-xml.sha256"), ("LEGACY_VIRTUAL", "virtual.json")):
        if hasattr(module, name):
            setattr(module, name, os.path.join(d, base))
    module.MONITORS_XML = os.path.join(d, "monitors.xml")
    return d


# No test may touch the real display: every case that needs Mutter replaces
# gvd.proxy with a fake. Same for the root helper, Sunshine, gsettings and the
# Xwayland diagnosis (it reads /proc and runs eu-stack).
gvd.proxy = _forbidden
gvd.helper = _forbidden
gvd.restart_sunshine = _forbidden
gvd.gsettings = _forbidden
gvd.xwayland_frozen = _forbidden
gvd.sunshine_journal = lambda *a, **k: None  # never the real journal
_journal = []
gvd.journal = _journal.append  # never the real syslog
isolate(gvd)

FAILED = []


def check(label, got, want):
    if got != want:
        FAILED.append(f"{label}: {got!r} instead of {want!r}")
        print(f"KO  {label}\n      got  {got!r}\n      want {want!r}")
    else:
        print(f"ok  {label}")


# --- 0. test configuration ------------------------------------------------


def _put_timing(e, offset, width, height, refresh, hblank=80, vblank=50):
    """Writes a detailed timing descriptor into an EDID being built."""
    clock = round(refresh * (width + hblank) * (height + vblank) / 10_000)
    e[offset] = clock & 0xFF
    e[offset + 1] = clock >> 8
    e[offset + 2] = width & 0xFF
    e[offset + 3] = hblank & 0xFF
    e[offset + 4] = (width >> 8) << 4 | hblank >> 8
    e[offset + 5] = height & 0xFF
    e[offset + 6] = vblank & 0xFF
    e[offset + 7] = (height >> 8) << 4 | vblank >> 8


def _fix_checksum(e):
    e[127] = (-sum(e[:127])) % 256


def make_edid(width=2400, height=1600, refresh=120.0, product="GVD-VIRTUAL", checksum=True):
    """A minimal EDID 1.4 base block, built in memory: one detailed timing
    (the native mode) and, optionally, a product name descriptor."""
    e = bytearray(128)
    e[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    e[8:10] = b"\x5a\x54"  # manufacturer ID "VRT"
    e[18], e[19] = 1, 4  # EDID 1.4
    _put_timing(e, 54, width, height, refresh)
    if product is not None:
        name = product.encode("ascii")[:13]
        if len(name) < 13:
            name += b"\n" + b" " * (12 - len(name))
        e[72:77] = b"\x00\x00\x00\xfc\x00"
        e[77:90] = name
    for offset in (90, 108):
        if not any(e[offset:offset + 18]):
            e[offset + 3] = 0x10  # dummy descriptor
    _fix_checksum(e)
    if not checksum:
        e[127] = (e[127] + 1) % 256
    return bytes(e)


TEST_DIR = tempfile.mkdtemp()
EDID_FILE = os.path.join(TEST_DIR, "edid.bin")
with open(EDID_FILE, "wb") as _f:
    _f.write(make_edid())
# Injected configuration, no file: never the real Sunshine log nor helper.
TEST_CONFIG = {
    "connector": "DP-2",
    "edid": EDID_FILE,
    "sunshine_log": os.path.join(TEST_DIR, "absent.log"),
    "helper": os.path.join(TEST_DIR, "no-helper"),
}
gvd.configure(**TEST_CONFIG)


# Scales Mutter accepts on a 2400x1600 panel: it requires the width AND the
# height of the mode to divide by the scale (1.5 is refused).
SCALES_2400x1600 = [1.0, 1.25, 1.3333333730697632, 1.6666666269302368,
                    2.0, 2.5, 2.6666667461395264]


def mode(w, h, rate, current=False, preferred=False, scales=None):
    props = {}
    if current:
        props["is-current"] = True
    if preferred:
        props["is-preferred"] = True
    if scales is None:
        scales = SCALES_2400x1600 if (w, h) == (2400, 1600) else [1.0]
    # 4th field: preferred scale of the mode; 5th: accepted scales (Mutter).
    return (f"{w}x{h}@{rate:.3f}", w, h, rate, 1.0, list(scales), props)


def hdmi(w, h, rate=60.0):
    return mode(w, h, rate, current=True, preferred=True)


# Physical monitor DP-3 (portrait) and virtual connector DP-2 (2400x1600 panel).
DP3 = (("DP-3", "ABC", "PORTRAIT-24", "000000000001"), [hdmi(1920, 1080)], {})
DP2 = (("DP-2", "VRT", gvd.VIRTUAL_PRODUCT, "0x00000001"),
       [mode(2400, 1600, 120.0, preferred=True), mode(1920, 1200, 60.0)],
       {})


def with_virtual_state(scale=1.3333333730697632, transform=gvd.NORMAL, x=864, y=168):
    """Installs remembered preferences in a temporary state directory."""
    tmp = tempfile.mkdtemp()
    gvd.STATE_DIR = tmp
    gvd.PREFS_FILE = os.path.join(tmp, "prefs.json")
    gvd.MODE_FILE = os.path.join(tmp, "mode")
    gvd.LOCK_FILE = os.path.join(tmp, "lock")
    # never the real session marker: the watcher would back off
    gvd.SESSION_FILE = os.path.join(tmp, "session")
    gvd.MOUSE_FILE = os.path.join(tmp, "mouse")
    gvd.MONITORS_XML = os.path.join(tmp, "monitors.xml")
    gvd.XML_WRITTEN = os.path.join(tmp, "monitors-xml.sha256")
    gvd.load_prefs = _prefs_override
    _prefs.state = {
        "virtual": {"scale": scale, "transform": transform, "x": x, "y": y},
        "reals": {"DP-3": {"x": 0, "y": 0, "scale": 1.25, "transform": 1,
                           "primary": True, "mode": [1920, 1080, 60.0]}},
    }


class _Prefs:
    state = None


_prefs = _Prefs()


def _prefs_override():
    return copy.deepcopy(_prefs.state)


def virt_prefs(scale, transform=0):
    """Forces the remembered scale of the virtual monitor, for one test."""
    def load():
        p = _prefs_override()
        p["virtual"] = {"scale": scale, "transform": transform, "x": 0, "y": 0}
        return p
    return load


with_virtual_state()

# --- 1. groups ------------------------------------------------------------
G_DP3 = (0, 0, 1.25, 1, True, [DP3[0]])
G_VIRT = (864, 168, 1.3333333730697632, 0, False, [(("DP-2", "2400x1600@120.000", {}))])
G_MIRROR = (0, 0, 1.25, 1, True, [DP3[0], (("DP-2", "2400x1600@120.000", {}))])
G_VIRT_ALONE = (0, 0, 1.3333333730697632, 0, True, [(("DP-2", "2400x1600@120.000", {}))])

MONITORS = [DP3, DP2]

# --- 2. mirror ------------------------------------------------------------
check("mirror detected", bool(gvd.mirrored([G_MIRROR])), True)
check("extend is not a mirror", gvd.mirrored([G_DP3, G_VIRT]), [])
check("virtual alone is not a mirror", gvd.mirrored([G_VIRT_ALONE]), [])
try:
    gvd.apply(1, [G_MIRROR])
    check("apply refuses a mirror", "no error", "SystemExit")
except SystemExit:
    check("apply refuses a mirror", "SystemExit", "SystemExit")

# --- 3. base layout -------------------------------------------------------
layout = gvd.base_layout(MONITORS, [G_DP3, G_VIRT])
check("base: keeps DP-3 alone", [c[0] for c in layout[0][5]], ["DP-3"])
check("base: DP-3 portrait 1.25 primary",
      (layout[0][0], layout[0][1], layout[0][2], layout[0][3], layout[0][4]),
      (0, 0, 1.25, 1, True))
check("base: virtual removed", gvd.mirrored(layout), [])
check("base: nothing to do if already base", gvd.base_layout(MONITORS, [G_DP3]), None)

# base from `only`: DP-3, off, must come back with its remembered config
layout = gvd.base_layout(MONITORS, [G_VIRT_ALONE])
check("base from only: turns DP-3 back on", [c[0] for c in layout[0][5]], ["DP-3"])
check("base from only: portrait kept", layout[0][3], 1)

# base from a mirror: the virtual monitor leaves the group
layout = gvd.base_layout(MONITORS, [G_MIRROR])
check("base from mirror: DP-3 alone", [c[0] for c in layout[0][5]], ["DP-3"])

# Mutter fell back to its defaults (rotation lost, scale 1.0): the tool's
# memory must take over, otherwise the config is lost on reboot
# (`base` and `boot` restore; RESTORE is what they set)
G_DP3_DEFAULT = (0, 0, 1.0, 0, True, [DP3[0]])
gvd.RESTORE = True
layout = gvd.base_layout(MONITORS, [G_DP3_DEFAULT])
check("base: lost rotation restored",
      (layout[0][2], layout[0][3], layout[0][4]), (1.25, 1, True))
G_DP3_OTHER = (0, 0, 1.3333333730697632, 1, True, [DP3[0]])
layout = gvd.base_layout(MONITORS, [G_DP3_OTHER])
check("base: remembered scale restored", round(layout[0][2], 4), 1.25)
gvd.RESTORE = False

# outside streams the watcher never fights GNOME Settings: the live layout is kept
check("watcher outside a stream: a scale chosen in Settings is kept",
      gvd.base_layout(MONITORS, [G_DP3_OTHER]), None)

# --- 4. virtual alone -----------------------------------------------------
layout = gvd.solo_layout(MONITORS, [G_DP3, G_VIRT])
check("only: one group", len(layout), 1)
check("only: virtual primary", layout[0][4], True)
check("only: remembered scale", round(layout[0][2], 4), 1.3333)
check("only: nothing to do if already alone", gvd.solo_layout(MONITORS, [G_VIRT_ALONE]), None)

# --- 4b. remote (virtual alone at 60 Hz) ----------------------------------
DP2_60 = (DP2[0], [mode(2400, 1600, 120.0, preferred=True), mode(2400, 1600, 60.0),
                   mode(1920, 1200, 60.0)], {})
DP2_AT_60 = (DP2[0], [mode(2400, 1600, 120.0, preferred=True),
                      mode(2400, 1600, 60.0, current=True)], {})
M_60 = [DP3, DP2_60]
G_VIRT_60 = (0, 0, 1.3333333730697632, 0, True, [(("DP-2", "2400x1600@60.000", {}))])
layout = gvd.solo_layout(M_60, [G_DP3], remote=True)
check("remote: one group, virtual primary", (len(layout), layout[0][4]), (1, True))
check("remote: 2400x1600 at 60 Hz", layout[0][5][0][1], "2400x1600@60.000")
check("remote: remembered scale", round(layout[0][2], 4), 1.3333)
check("remote: nothing to do if already at 60 Hz",
      gvd.solo_layout([DP3, DP2_AT_60], [G_VIRT_60], remote=True), None)
layout = gvd.solo_layout(M_60, [G_VIRT_ALONE], remote=True)
check("remote from only at 120 Hz: switches to 60 Hz", layout[0][5][0][1], "2400x1600@60.000")
layout = gvd.solo_layout([DP3, DP2_AT_60], [G_VIRT_60])
check("only from remote: back to 120 Hz", layout[0][5][0][1], "2400x1600@120.000")
check("only from remote: scale kept", round(layout[0][2], 4), 1.3333)
check("only: 120 Hz already set, nothing to do", gvd.solo_layout(M_60, [G_VIRT_ALONE]), None)
check("remote without a 60 Hz mode: nothing", gvd.solo_layout([DP3, (DP2[0], [mode(2400, 1600, 120.0, preferred=True)], {})], [G_DP3], remote=True), None)
check("remote: check refuses 120 Hz",
      gvd.settled("remote", M_60, [G_VIRT_ALONE]) is not None, True)
check("remote: check accepts 60 Hz", gvd.settled("remote", [DP3, DP2_AT_60], [G_VIRT_60]), None)
gvd.write_mode("remote")
check("remote: stored mode read back", gvd.read_mode(), "remote")
gvd.write_mode("base")

# extend right after `remote`: the virtual monitor goes back to 120 Hz
layout = gvd.build_layout([DP3, DP2_AT_60], [G_DP3, (864, 168, 1.3333333730697632, 0, True, [(("DP-2", "2400x1600@60.000", {}))])])
check("extend from remote: back to 120 Hz", layout[-1][5][0][1] if layout else None, "2400x1600@120.000")

# rotations: one table, "upside_down" (monitors.xml) and "inverted" (command)
check("rotation: upside_down read", gvd.ROTATIONS["upside_down"], 2)
check("rotation: inverted accepted", gvd.ROTATIONS["inverted"], 2)
check("rotation: written as upside_down", gvd.ROTATION_NAMES[2], "upside_down")

# physical monitor unplugged: base keeps the virtual monitor, without error
check("base without a physical monitor: accepted", gvd.settled("base", [DP2], [G_VIRT_ALONE]), None)
check("base with DP-3 connected: the virtual monitor must turn off", gvd.settled("base", MONITORS, [G_DP3, G_VIRT]) is not None, True)

# --- 5. extend ------------------------------------------------------------
G_DP3_SEC = (0, 0, 1.25, 1, False, [DP3[0]])
G_VIRT_PRIM = (864, 168, 1.3333333730697632, 0, True,
               [(("DP-2", "2400x1600@120.000", {}))])
check("extend: nothing to fix if the virtual monitor is primary",
      gvd.build_layout(MONITORS, [G_DP3_SEC, G_VIRT_PRIM]), None)

# the virtual monitor must be primary even if the layout is otherwise right
layout = gvd.build_layout(MONITORS, [G_DP3, G_VIRT])
check("extend: virtual made primary", layout[1][4], True)
check("extend: DP-3 made secondary", layout[0][4], False)

# virtual monitor off (base): turned back on at the right, centred, primary
layout = gvd.build_layout(MONITORS, [G_DP3])
check("extend from base: 2 groups", len(layout), 2)
check("extend from base: DP-3 keeps its geometry", layout[0][:4], (0, 0, 1.25, 1))
check("extend from base: DP-3 no longer primary", layout[0][4], False)
check("extend from base: DP-2 placed and primary",
      (layout[1][0], layout[1][1], round(layout[1][2], 4), layout[1][3], layout[1][4]),
      (864, 168, 1.3333, 0, True))

# remembered scale (1.6667, the one in memory) and a valid placement: left
# alone, even if not centred, since it is the user's setting.
gvd.load_prefs = virt_prefs(1.6666666269302368)
layout = gvd.build_layout(MONITORS, [(0, 0, 1.25, 1, True, [DP3[0]]),
                                     (864, 0, 1.6666666269302368, 0, False,
                                      [(("DP-2", "2400x1600@120.000", {}))])])
check("extend: remembered scale kept",
      round(layout[1][2], 4), 1.6667)

layout = gvd.build_layout(MONITORS, [(0, 0, 1.25, 1, False, [DP3[0]]),
                                     (864, 0, 1.6666666269302368, 0, True,
                                      [(("DP-2", "2400x1600@120.000", {}))])])
check("extend: remembered layout respected when already right", layout, None)

# same scale, but on top of DP-3: position fixed, scale kept
layout = gvd.build_layout(MONITORS, [(0, 0, 1.25, 1, True, [DP3[0]]),
                                     (10, 10, 1.6666666269302368, 0, False,
                                      [(("DP-2", "2400x1600@120.000", {}))])])
check("extend: scale kept when the position is fixed", round(layout[1][2], 4), 1.6667)
check("extend: vertical centring", layout[1][1], int(round((1536 - 1600 / 1.6666666269302368) / 2)))
gvd.load_prefs = _prefs_override

# virtual monitor mirroring DP-3: split
layout = gvd.build_layout(MONITORS, [G_MIRROR])
check("extend from mirror: 2 groups", len(layout), 2)
check("extend from mirror: no mirror left", gvd.mirrored(layout), [])
check("extend from mirror: DP-2 on the right", layout[1][0] >= 864, True)
check("extend from mirror: virtual primary", layout[1][4], True)

# virtual monitor detached (far away, no shared edge): brought closer
layout = gvd.build_layout(MONITORS, [(0, 0, 1.25, 1, True, [DP3[0]]),
                                     (3000, 3000, 1.3333333730697632, 0, False,
                                      [(("DP-2", "2400x1600@120.000", {}))])])
check("extend: detached virtual monitor brought closer", layout[1][0], 864)

# virtual monitor overlapping DP-3: moved
layout = gvd.build_layout(MONITORS, [(0, 0, 1.25, 1, True, [DP3[0]]),
                                     (10, 10, 1.3333333730697632, 0, False,
                                      [(("DP-2", "2400x1600@120.000", {}))])])
check("extend: overlap fixed", layout[1][0], 864)

# virtual monitor alone while extend is requested: DP-3 turned back on
layout = gvd.build_layout(MONITORS, [G_VIRT_ALONE])
check("extend from only: DP-3 turned back on", [sorted(c[0] for c in g[5]) for g in layout],
      [["DP-3"], ["DP-2"]])
check("extend from only: virtual primary", layout[1][4], True)

# virtual connector missing: nothing invented
check("extend without a virtual connector", gvd.build_layout([DP3], [(0, 0, 1.25, 1, True, [DP3[0]])]), None)

# freshly plugged connector: Mutter turns it on at its default scale (1.6667
# here), which is not the user's choice. The remembered scale (1.25) must take
# over, otherwise the stream starts offset.
G_VIRT_DEFAULT = (1080, 480, 1.6666666269302368, gvd.NORMAL, True,
                  [(("DP-2", "2400x1600@120.000", {}))])
gvd.load_prefs = virt_prefs(1.25)
layout = gvd.build_layout(MONITORS, [G_DP3, G_VIRT_DEFAULT])
check("new virtual monitor: remembered scale", round(layout[1][2], 4), 1.25)
check("new virtual monitor: placed and centred",
      (layout[1][0], layout[1][1]), (864, 128))

# remembered state: virtual primary, DP-3 secondary, nothing to fix
check("remembered layout: intact",
      gvd.build_layout(MONITORS, [(0, 0, 1.25, 1, False, [DP3[0]]),
                                  (864, 128, 1.25, gvd.NORMAL, True,
                                   [(("DP-2", "2400x1600@120.000", {}))])]),
      None)
# but if DP-3 stayed primary, it is fixed
check("DP-3 still primary: fixed",
      gvd.build_layout(MONITORS, [(0, 0, 1.25, 1, True, [DP3[0]]),
                                  (864, 128, 1.25, gvd.NORMAL, True,
                                   [(("DP-2", "2400x1600@120.000", {}))])])[1][4],
      True)

# a remembered scale that does not fit the current mode must not be forced
# (Mutter would refuse it, "Scale 1.5 not valid"): the observed one is kept
gvd.load_prefs = virt_prefs(1.5)
layout = gvd.build_layout(MONITORS, [G_DP3, G_VIRT_DEFAULT])
check("invalid scale ignored", round(layout[1][2], 4), 1.6667)

# remembered scale valid for another mode than the current one: refused too
gvd.load_prefs = virt_prefs(2.5)
vmon_1080 = [DP2[0], [mode(2400, 1600, 120.0, current=True, preferred=True,
                           scales=[1.0, 1.25, 1.3333333730697632, 2.0])], {}]
layout = gvd.build_layout([DP3, vmon_1080], [G_DP3, G_VIRT_DEFAULT])
check("scale outside the list ignored", round(layout[1][2], 4), 1.6667)
gvd.load_prefs = _prefs_override

# `prep` (Sunshine's prep-cmd) must plug the virtual connector if it is
# missing: after a reboot the connector is gone, and without it the mode
# cannot be applied. Sunshine then refuses to launch the app and Moonlight
# shows "failed to start".
_seen = {}
_real_command, _real_ensure, _real_mark = gvd.command, gvd.ensure_virtual, gvd.mark_session
_real_match = gvd.match_client_mode
gvd.command = lambda mode, before=None: _seen.update(mode=mode, before=before) or 0
gvd.ensure_virtual = lambda *a, **k: _seen.update(plugged=True)
gvd.match_client_mode = lambda m: _seen.update(matched=m)
gvd.mark_session = lambda *a, **k: None


def _prep(mode):
    _seen.clear()
    gvd.cmd_prep(mode)
    _seen["before"]()  # what command() runs before applying the mode
    return _seen


gvd.cmd_prep("extend")
check("prep extend: plugs the connector", _prep("extend").get("plugged"), True)
check("prep extend: matches the client first", _seen.get("matched"), "extend")
check("prep only: plugs the connector", _prep("only").get("plugged"), True)
check("prep only: right mode", _seen.get("mode"), "only")
check("prep remote: remote mode, connector plugged",
      (_prep("remote").get("mode"), _seen.get("plugged"), _seen.get("matched")),
      ("remote", True, "remote"))
gvd.command, gvd.ensure_virtual, gvd.mark_session = _real_command, _real_ensure, _real_mark
gvd.match_client_mode = _real_match

# `boot` (at login) plugs the connector, then applies base, except during a stream
_seen.clear()
gvd.command = lambda mode, before=None: _seen.update(mode=mode, before=before) or 0
gvd.ensure_virtual = lambda *a, **k: None
_real_open, _real_wait, _real_marked = gvd.stream_open, gvd.wait_running, gvd.session_marked
gvd.stream_open = gvd.wait_running = gvd.session_marked = lambda: False
gvd.cmd_boot()
check("boot: base applied", _seen.get("mode"), "base")
_seen.clear()
gvd.stream_open = lambda: True
gvd.cmd_boot()
check("boot during a stream: base left alone", _seen.get("mode"), None)
gvd.stream_open = lambda: False


# and if the kernel refuses, say so without blocking base
def _boom(*a, **k):
    raise SystemExit("no access")


gvd.ensure_virtual = _boom
check("boot: goes on without the virtual monitor", gvd.cmd_boot(), 0)
gvd.command, gvd.ensure_virtual = _real_command, _real_ensure
gvd.stream_open, gvd.wait_running, gvd.session_marked = _real_open, _real_wait, _real_marked

# mouse: "flat" profile during a stream, previous setting restored at the end
_gs = {"accel-profile": "'default'"}
_real_gsettings = gvd.gsettings


def _fake_gsettings(*args):
    if args[0] == "get":
        return _gs[args[2]]
    _gs[args[2]] = f"'{args[3]}'"
    return ""


gvd.gsettings = _fake_gsettings
gvd.flatten_mouse()
check("mouse: flat during the stream", _gs["accel-profile"], "'flat'")
gvd.flatten_mouse()  # only -> remote: the original setting stays remembered
gvd.restore_mouse()
check("mouse: previous setting restored at the end", _gs["accel-profile"], "'default'")
_gs["accel-profile"] = "'flat'"
gvd.flatten_mouse(); gvd.restore_mouse()
check("mouse: a flat profile chosen by the user stays flat", _gs["accel-profile"], "'flat'")
gvd.restore_mouse()
check("mouse: nothing to restore outside a stream", _gs["accel-profile"], "'flat'")
gvd.gsettings = _real_gsettings

# --- 6. persistence -------------------------------------------------------
tmp = tempfile.mkdtemp()
gvd.MONITORS_XML = os.path.join(tmp, "monitors.xml")
_serial, monitors, logical = 1, MONITORS, [G_DP3, G_VIRT]
check("snapshot written", gvd.snapshot_xml(monitors, logical), True)
root = ET.parse(gvd.MONITORS_XML).getroot()
check("snapshot: a single block", len(root.findall("configuration")), 1)
lms = root.findall("configuration/logicalmonitor")
# The virtual monitor only exists once its EDID is injected: if it were
# listed here, Mutter would not find this configuration at boot and would
# start from its defaults (that is how the rotation got lost).
check("snapshot: only the physical monitor", len(lms), 1)
check("snapshot: DP-3 portrait primary",
      (lms[0].findtext("monitor/monitorspec/connector"), lms[0].findtext("transform/rotation"),
       lms[0].findtext("primary")), ("DP-3", "left", "yes"))
# ... and not as <disabled> either: Mutter keys its configurations on every
# monitor listed, disabled ones included. A DP-2 entry would make the
# configuration impossible to find at boot, the very same bug.
check("snapshot: virtual monitor absent", root.find("configuration/disabled"), None)
check("snapshot: nothing to rewrite", gvd.snapshot_xml(monitors, logical), False)

# in `only` mode (only the virtual monitor on), the file keeps the physical config
gvd.snapshot_xml(MONITORS, [G_VIRT_ALONE])
root = ET.parse(gvd.MONITORS_XML).getroot()
check("snapshot only: physical monitor kept",
      root.findtext("configuration/logicalmonitor/monitor/monitorspec/connector"), "DP-3")
check("snapshot only: portrait kept",
      root.findtext("configuration/logicalmonitor/transform/rotation"), "left")

# in base, the virtual monitor is connected but off: it must not appear
gvd.snapshot_xml(MONITORS, [(0, 0, 1.25, 1, True, [DP3[0]])])
root = ET.parse(gvd.MONITORS_XML).getroot()
check("snapshot base: one active monitor", len(root.findall("configuration/logicalmonitor")), 1)
check("snapshot base: nothing disabled", root.find("configuration/disabled"), None)

# a physical monitor connected but off is saved as "disabled" (it will be
# there at boot); the virtual monitor stays absent
HDMI = (("HDMI-A-1", "XYZ", "WIDE-29", "0"), [hdmi(2560, 1080)], {})
gvd.snapshot_xml(MONITORS + [HDMI], [(0, 0, 1.25, 1, True, [DP3[0]])])
root = ET.parse(gvd.MONITORS_XML).getroot()
check("snapshot: physical monitor off as disabled",
      [d.findtext("monitorspec/connector") for d in root.findall("configuration/disabled")],
      ["HDMI-A-1"])

# a mirrored state must never be saved
before = open(gvd.MONITORS_XML, "rb").read()
check("snapshot refuses a mirror", gvd.snapshot_xml(MONITORS, [G_MIRROR]), False)
check("snapshot refused: file intact", open(gvd.MONITORS_XML, "rb").read(), before)

# --- 7. Sunshine session --------------------------------------------------
LOG = os.path.join(tmp, "sunshine.log")
gvd.SUNSHINE_LOG = LOG
PREP = "/usr/local/bin/gnome-virtual-display prep"
UNDO = "/usr/local/bin/gnome-virtual-display base"


def log(*lines):
    with open(LOG, "w") as f:
        f.write("\n".join(lines) + "\n")


log("[2025-01-10 12:00:00.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:00:00.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:00:00.000]: Info: Mapped 'DP-2' to kmsgrab monitor index 0",
    "[2025-01-10 12:05:00.000]: Info: CLIENT DISCONNECTED")
check("session closed", gvd.stream_open(), False)
check("right monitor streamed", gvd.captured_wrong_screen(), False)

log(f"[2025-01-10 12:00:00.000]: Info: Executing Do Cmd: [{PREP} extend]",
    "[2025-01-10 12:00:01.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:00:00.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:00:00.000]: Info: Mapped 'DP-3' to kmsgrab monitor index 0",
    "[2025-01-10 12:05:00.000]: Info: CLIENT DISCONNECTED")
check("wrong monitor streamed", gvd.captured_wrong_screen(), True)

# "Desktop" stream: no app, streaming DP-3 is normal
log("[2025-01-10 12:00:00.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:00:00.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:00:00.000]: Info: Mapped 'DP-3' to kmsgrab monitor index 0",
    "[2025-01-10 12:05:00.000]: Info: CLIENT DISCONNECTED")
check("desktop stream: not an anomaly", gvd.captured_wrong_screen(), False)

log("[2025-01-10 12:00:00.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:00:00.000]: Info: CLIENT CONNECTED")
check("session running", gvd.stream_open(), True)
check("session running: no verdict", gvd.captured_wrong_screen(), False)

log("[2025-01-10 12:00:00.000]: Info: Sunshine version: test")
check("fresh log: no conclusion", gvd.captured_wrong_screen(), False)

# app stuck: the next session starts without a Do Cmd, and no Undo came to
# free the app after the previous one
log(f"[2025-01-10 12:00:00.000]: Info: Executing Do Cmd: [{PREP} extend]",
    "[2025-01-10 12:00:01.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:00:01.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:00:01.000]: Info: Mapped 'DP-2' to kmsgrab monitor index 0",
    "[2025-01-10 12:00:30.000]: Info: CLIENT DISCONNECTED",
    "[2025-01-10 12:01:00.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:01:00.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:01:00.000]: Info: Mapped 'DP-3' to kmsgrab monitor index 0")
check("stuck app detected", gvd.sunshine_stuck(), True)

# app left open, but not ours: the client may have asked for the "Desktop"
# entry, its session is legitimate, nothing is cut
log("[2025-01-10 12:00:00.000]: Info: Executing Do Cmd: [notepad.exe]",
    "[2025-01-10 12:00:01.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:00:01.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:00:30.000]: Info: CLIENT DISCONNECTED",
    "[2025-01-10 12:01:00.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:01:00.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:01:00.000]: Info: Mapped 'DP-3' to kmsgrab monitor index 0")
check("foreign app stuck: no restart", gvd.stuck_own_app(), False)

# ours, never freed, and a session that starts again without prep: the client
# sees DP-3, Sunshine must be restarted
log(f"[2025-01-10 12:00:00.000]: Info: Executing Do Cmd: [{PREP} extend]",
    "[2025-01-10 12:00:01.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:00:01.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:00:30.000]: Info: CLIENT DISCONNECTED",
    "[2025-01-10 12:01:00.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:01:00.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:01:00.000]: Info: Mapped 'DP-3' to kmsgrab monitor index 0")
check("our app stuck: restart", gvd.stuck_own_app(), True)

# app freed cleanly by an Undo: the next stream is the desktop, normal
log(f"[2025-01-10 12:00:00.000]: Info: Executing Do Cmd: [{PREP} extend]",
    "[2025-01-10 12:00:01.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:00:01.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:00:30.000]: Info: CLIENT DISCONNECTED",
    "[2025-01-10 12:00:31.000]: Info: Executing Undo Cmd: [gnome-virtual-display base]",
    "[2025-01-10 12:01:00.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:01:00.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:01:00.000]: Info: Mapped 'DP-3' to kmsgrab monitor index 0")
check("app freed: not stuck", gvd.sunshine_stuck(), False)

# healthy session running: the Do Cmd does come before the session start
log(f"[2025-01-10 12:00:00.000]: Info: Executing Do Cmd: [{PREP} extend]",
    "[2025-01-10 12:00:01.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:00:01.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:00:01.000]: Info: Mapped 'DP-2' to kmsgrab monitor index 0")
check("healthy session: not stuck", gvd.sunshine_stuck(), False)

# desktop stream alone: no Do Cmd, nothing to restart
log("[2025-01-10 12:00:00.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:00:00.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:00:00.000]: Info: Mapped 'DP-3' to kmsgrab monitor index 0")
check("desktop stream: not stuck", gvd.sunshine_stuck(), False)

# app still "running" for Sunshine: our Do Cmd with no Undo after it
log(f"[2025-01-11 01:00:00.000]: Info: Executing Do Cmd: [{PREP} only]",
    "[2025-01-11 01:00:01.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-11 01:05:00.000]: Info: CLIENT DISCONNECTED")
check("client left without quitting: app engaged", gvd.app_engaged(), True)
log(f"[2025-01-11 01:00:00.000]: Info: Executing Do Cmd: [{PREP} only]",
    "[2025-01-11 01:00:01.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-11 01:05:00.000]: Info: CLIENT DISCONNECTED",
    f"[2025-01-11 01:05:00.100]: Info: Executing Undo Cmd: [{UNDO}]")
check("client quit the app: freed", gvd.app_engaged(), False)
log("[2025-01-11 01:00:00.000]: Info: Executing Do Cmd: [xrandr --output HDMI-1 --mode 1920x1080]",
    "[2025-01-11 01:05:00.000]: Info: CLIENT DISCONNECTED")
check("foreign app: not ours", gvd.app_engaged(), False)
log("[2025-01-11 01:00:00.000]: Info: Sunshine version: test")
check("Sunshine restarted (fresh log): nothing engaged", gvd.app_engaged(), False)

# end through `quit`: Sunshine runs the undo without "CLIENT DISCONNECTED"
log(f"[2025-01-11 02:07:41.663]: Info: Executing Do Cmd: [{PREP} remote]",
    "[2025-01-11 02:07:42.882]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-11 02:07:42.931]: Info: CLIENT CONNECTED",
    "[2025-01-11 02:07:43.805]: Info: New streaming session started [active sessions: 2]",
    "[2025-01-11 02:07:43.827]: Info: CLIENT CONNECTED",
    f"[2025-01-11 02:08:47.610]: Info: Executing Undo Cmd: [{UNDO}]")
check("quit without DISCONNECTED: no stream left", gvd.stream_open(), False)
check("quit without DISCONNECTED: app freed", gvd.app_engaged(), False)
check("quit without DISCONNECTED: idle since the undo", (gvd.idle_for() or 0) > 60, True)

# session token: the most recent stream wins
t1 = gvd.mark_session()
check("token read back", gvd.session_token(), t1)
check("session marked (decimal token)", gvd.session_marked(), True)
t2 = gvd.mark_session()
check("new prep: new token", t1 != t2 and gvd.session_token() == t2, True)
gvd.clear_session()
check("base: token cleared", gvd.session_token(), None)

gvd.SUNSHINE_LOG = os.path.join(tmp, "absent.log")
check("missing log: a session is assumed", gvd.stream_open(), True)

# --- 7b. every documented command is recognised ---------------------------
# (a command dropped from the list by mistake printed the help instead of
# running, for example `reapply` when Sunshine starts)
_doc_actions = sorted(set(re.findall(r"^  gnome-virtual-display ([a-z-]+)", gvd.__doc__, re.M)))
_called = []
_saved_cmds = {n: getattr(gvd, n) for n in dir(gvd) if n.startswith("cmd_")}
for _name in _saved_cmds:
    setattr(gvd, _name, (lambda n: (lambda *a, **k: _called.append(n) or 0))(_name))
_real_lock = gvd.lock
gvd.lock = lambda *a, **k: None
_usage_hits = []
_real_usage = gvd.usage
gvd.usage = lambda: _usage_hits.append(1) or 2
for _act in _doc_actions:
    gvd.main(["gnome-virtual-display", _act])
check("every documented command recognised", (len(_doc_actions) >= 14, _usage_hits), (True, []))
gvd.usage, gvd.lock = _real_usage, _real_lock
for _name, _fn in _saved_cmds.items():
    setattr(gvd, _name, _fn)

# --- 8. frozen gnome-shell ------------------------------------------------
# Mutter no longer answers: short timeout, cause given, code 3, and above all
# no hotplug (present() must not conclude "connector missing").
from gi.repository import Gio, GLib  # noqa: E402


class _FrozenProxy:
    calls = 0

    def get_name_owner(self):
        return ":1.52"

    def call_sync(self, *a, **k):
        _FrozenProxy.calls += 1
        raise GLib.Error.new_literal(Gio.io_error_quark(), "Timeout was reached",
                                     Gio.IOErrorEnum.TIMED_OUT)


_real_proxy, _real_diag, _real_helper = gvd.proxy, gvd.xwayland_frozen, gvd.helper
gvd.proxy = lambda: _FrozenProxy()
gvd.xwayland_frozen = lambda: gvd.XwaylandFreeze("gnome-shell does not answer: Xwayland (pid 1) is looping in xwl_present_flush_blocked")
try:
    gvd.state()
    check("frozen: state raises SessionUnreachable", "nothing", "SessionUnreachable")
except gvd.SessionUnreachable as e:
    check("frozen: state raises SessionUnreachable", "SessionUnreachable", "SessionUnreachable")
    check("frozen: the cause is given", "xwl_present_flush_blocked" in str(e), True)
    check("frozen: exit code 3", e.code, 3)
check("frozen: signature seen, no second try", _FrozenProxy.calls, 1)

_hotplug = []
gvd.helper = lambda action: _hotplug.append(action)
try:
    gvd.ensure_virtual(wait=0)
except gvd.SessionUnreachable:
    pass
check("frozen: no hotplug", _hotplug, [])

# without the signature: a second try, then give up
_FrozenProxy.calls = 0
gvd.xwayland_frozen = lambda: None
try:
    gvd.state()
except gvd.SessionUnreachable as e:
    check("slow without signature: two tries", _FrozenProxy.calls, 2)


# an error that is not a timeout (config refused) goes up as is
class _RefusingProxy:
    def get_name_owner(self):
        return ":1.52"

    def call_sync(self, *a, **k):
        raise GLib.Error.new_literal(Gio.dbus_error_quark(), "Logical monitors not adjacent",
                                     Gio.DBusError.INVALID_ARGS)


gvd.proxy = lambda: _RefusingProxy()
try:
    gvd.mutter_call("ApplyMonitorsConfig")
except gvd.SessionUnreachable:
    check("config refused: not mistaken for a freeze", "SessionUnreachable", "GLib.Error")
except GLib.Error:
    check("config refused: not mistaken for a freeze", "GLib.Error", "GLib.Error")

gvd.proxy = lambda: _FrozenProxy()
gvd.xwayland_frozen = lambda: gvd.XwaylandFreeze("cause")
_jr = []
_real_journal = gvd.journal
gvd.journal = lambda m: _jr.append(m)
check("frozen: prep exits with code 3", gvd.run(["gnome-virtual-display", "prep", "only"]), 3)
check("frozen: logged in the journal", bool(_jr) and _jr[0].startswith("prep only"), True)

# Xwayland busy but without the bug's signature (a game, say): second try
_FrozenProxy.calls = 0
gvd.proxy = lambda: _FrozenProxy()
gvd.xwayland_frozen = lambda: gvd.SessionUnreachable("Xwayland at 100 %, probably frozen")
try:
    gvd.state()
except gvd.XwaylandFreeze:
    check("Xwayland busy: not taken for the freeze", "XwaylandFreeze", "SessionUnreachable")
except gvd.SessionUnreachable as e:
    check("Xwayland busy: second try done", _FrozenProxy.calls, 2)
    check("Xwayland busy: cause given", "100 %" in str(e), True)


# Mutter refuses once (outdated state after a hotplug): converge retries
class _Flaky:
    def __init__(self):
        self.applied = 0
        self.state = [G_DP3]

    def get_name_owner(self):
        return ":1.52"

    def call_sync(self, method, arg, *a):
        if method == "GetCurrentState":
            return GLib.Variant("(ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})a{sv})",
                                (1, [], [], {}))
        self.applied += 1
        if self.applied == 1:
            raise GLib.Error.new_literal(Gio.dbus_error_quark(), "stale information",
                                         Gio.DBusError.ACCESS_DENIED)


_fl = _Flaky()
_real_state, _real_base_layout = gvd.state, gvd.base_layout
_states = iter([(1, MONITORS, [G_VIRT_ALONE]), (2, MONITORS, [G_VIRT_ALONE]),
                (3, MONITORS, [G_DP3]), (3, MONITORS, [G_DP3]), (3, MONITORS, [G_DP3])])
gvd.state = lambda: next(_states)
gvd.proxy = lambda: _fl
try:
    mons, logi = gvd.converge("base", tries=3, delay=0)
    check("config refused once: retried", (_fl.applied, [c[0] for c in logi[0][5]]), (2, ["DP-3"]))
except GLib.Error as e:
    check("config refused once: retried", e.message, "no error")
gvd.state = _real_state


# Login screen (no gnome-shell on the bus): Sunshine's commands let the stream
# through, so that the password can be typed remotely.
class _NoShellProxy:
    # Like the real GDBusProxy without an owner (checked on a private bus): no
    # owner, and a call fails locally.
    def get_name_owner(self):
        return None

    def call_sync(self, *a, **k):
        raise GLib.Error.new_literal(Gio.io_error_quark(), "Cannot invoke method; proxy is "
                                     "for the well-known name without an owner",
                                     Gio.IOErrorEnum.FAILED)


gvd.proxy = lambda: _NoShellProxy()
_hotplug.clear()
try:
    gvd.state()
except gvd.NoSession:
    check("login screen: NoSession", True, True)
for args, want in ((["prep", "only"], 0), (["prep", "remote"], 0), (["wait", "only"], 0),
                   (["base"], 0), (["list"], 3), (["only"], 3)):
    check(f"login screen: {' '.join(args)} exits with {want}", gvd.run(["gnome-virtual-display"] + args), want)
check("login screen: no hotplug", _hotplug, [])

# Automatic repair: only the Xwayland freeze, confirmed, once.
d = {"since": None, "until": 0.0, "freeze": 0, "freeze_since": None, "restarted": False}
g = gvd.XwaylandFreeze("freeze")
check("freeze: first sight, wait", gvd.should_restart_gdm(d, g, 1000), False)
check("freeze: 3 sights in 30 s, wait", [gvd.should_restart_gdm(d, g, t) for t in (1015, 1030)], [False, False])
check("freeze: 4th sight at 70 s, restart", gvd.should_restart_gdm(d, g, 1070), True)
check("freeze: a single restart", gvd.should_restart_gdm(d, g, 1100), False)
d = {"since": None, "until": 0.0, "freeze": 0, "freeze_since": None, "restarted": False}
for t in (0, 40, 80):
    r = gvd.should_restart_gdm(d, gvd.SessionUnreachable("slow"), t)
check("freeze: without signature, never a restart", r, False)
d = {"since": None, "until": 0.0, "freeze": 0, "freeze_since": None, "restarted": False}
gvd.should_restart_gdm(d, g, 0); gvd.should_restart_gdm(d, g, 40)
gvd.should_restart_gdm(d, gvd.SessionUnreachable("slow"), 50)
check("freeze: signature interrupted, counter reset", gvd.should_restart_gdm(d, g, 90), False)
# any other error leaves a trace and a non-zero code
_jr.clear()
_real_list = gvd.cmd_list


def _boom_list():
    raise ValueError("boom")


gvd.cmd_list = _boom_list
check("unexpected error: code 1", gvd.run(["gnome-virtual-display", "list"]), 1)
check("unexpected error: logged", bool(_jr) and "ValueError: boom" in _jr[-1], True)
gvd.cmd_list = _real_list
gvd.proxy, gvd.xwayland_frozen, gvd.helper, gvd.journal = _real_proxy, _real_diag, _real_helper, _real_journal

# ==========================================================================
# Cases added with the configuration file and the English names.
# ==========================================================================

# --- 9. EDID reader -------------------------------------------------------
info = gvd.parse_edid(make_edid())
check("edid: product name read", info.product, "GVD-VIRTUAL")
check("edid: native size from the first detailed timing", (info.width, info.height), (2400, 1600))
check("edid: refresh rate of the native mode", round(info.refresh, 2), 120.0)
check("edid: product name identifies the virtual monitor", gvd.VIRTUAL_PRODUCT, "GVD-VIRTUAL")
check("edid: native size used by best_mode", gvd.VIRTUAL_NATIVE, (2400, 1600))
info = gvd.parse_edid(make_edid(1920, 1080, 60.0, product="ABCDEFGHIJKLM"))
check("edid: other native size", (info.width, info.height, round(info.refresh, 2)), (1920, 1080, 60.0))
check("edid: 13-character name, no terminator", info.product, "ABCDEFGHIJKLM")
check("edid: no name descriptor", gvd.parse_edid(make_edid(product=None)).product, None)

# two detailed timings (120 Hz then 60 Hz, as in real panels): the first wins
_two = bytearray(make_edid())
_put_timing(_two, 90, 2400, 1600, 60.0, vblank=1700)
_fix_checksum(_two)
info = gvd.parse_edid(_two)
check("edid: first detailed timing wins", (info.width, info.height, round(info.refresh, 2)),
      (2400, 1600, 120.0))
check("edid: name found after a timing", info.product, "GVD-VIRTUAL")

for _bad, _label in ((make_edid(checksum=False), "bad checksum"),
                     (make_edid()[:100], "truncated block"),
                     (b"\x00" * 128, "no header")):
    try:
        gvd.parse_edid(_bad)
        check(f"edid: {_label} refused", "accepted", "ValueError")
    except ValueError:
        check(f"edid: {_label} refused", "ValueError", "ValueError")
check("edid: missing file", gvd.read_edid(os.path.join(TEST_DIR, "missing.bin")), None)

# a monitor with neither a current nor a preferred mode: the highest rate at
# the EDID's native size
NO_PREF = (DP2[0], [mode(2400, 1600, 60.0), mode(1920, 1200, 60.0), mode(2400, 1600, 120.0)], {})
check("edid: native fallback picks the highest rate", gvd.best_mode(NO_PREF)[0], "2400x1600@120.000")
OTHER = (("HDMI-A-2", "VRT", "GVD-VIRTUAL", "1"), [hdmi(1920, 1080)], {})
check("edid: product name recognised on another connector", gvd.is_virtual(OTHER), True)

# unreadable EDID: the virtual monitor is recognised by its connector only,
# and best_mode has no native size to fall back on
_bad_file = os.path.join(TEST_DIR, "bad-edid.bin")
with open(_bad_file, "wb") as _f:
    _f.write(b"not an EDID")
gvd.configure(**{**TEST_CONFIG, "edid": _bad_file})
check("unreadable edid: no product name", gvd.VIRTUAL_PRODUCT, None)
check("unreadable edid: no native size", gvd.VIRTUAL_NATIVE, None)
check("unreadable edid: virtual by its connector", gvd.is_virtual(DP2), True)
check("unreadable edid: product name alone is not enough", gvd.is_virtual(OTHER), False)
check("unreadable edid: a physical monitor stays physical", gvd.is_virtual(DP3), False)
check("unreadable edid: no native fallback", gvd.best_mode(NO_PREF), None)
check("unreadable edid: preferred mode still used", gvd.best_mode(DP2)[0], "2400x1600@120.000")
gvd.configure(**TEST_CONFIG)

# --- 10. legacy mode names ------------------------------------------------
for _old, _new in (("etendu", "extend"), ("seul", "only"), ("distance", "remote")):
    with open(gvd.MODE_FILE, "w") as _f:
        _f.write(_old + "\n")
    check(f"legacy mode file {_old!r} read as {_new!r}", gvd.read_mode(), _new)
for _name in gvd.MODES:
    gvd.write_mode(_name)
    check(f"mode {_name!r} written and read back", gvd.read_mode(), _name)
gvd.write_mode("only")
check("mode file written in English", open(gvd.MODE_FILE).read(), "only\n")
with open(gvd.MODE_FILE, "w") as _f:
    _f.write("sideways\n")
check("unknown mode read as base", gvd.read_mode(), "base")
gvd.write_mode("base")

# legacy names are also accepted as `prep` / `wait` arguments
_seen.clear()
gvd.command = lambda mode, before=None: _seen.update(mode=mode, before=before) or 0
gvd.mark_session = lambda *a, **k: None
for _old, _new in (("seul", "only"), ("distance", "remote"), ("etendu", "extend")):
    gvd.cmd_prep(_old)
    check(f"prep {_old}: mode {_new}", _seen.get("mode"), _new)
gvd.command, gvd.mark_session = _real_command, _real_mark
check("normalize_mode: English names unchanged",
      [gvd.normalize_mode(m) for m in gvd.MODES], list(gvd.MODES))

# --- 11. configuration file -----------------------------------------------
_cfg = os.path.join(TEST_DIR, "config.toml")
with open(_cfg, "w") as _f:
    _f.write('connector = "DP-1"\nfallback_scale = 2\ngrace = 30\n'
             'flat_mouse_during_stream = false\n')
_saved_env = os.environ.get("GVD_CONFIG")
os.environ["GVD_CONFIG"] = _cfg
check("config: $GVD_CONFIG names the file", gvd.config_path(), _cfg)
_settings = gvd.read_config_file(gvd.config_path())
check("config: TOML read", _settings,
      {"connector": "DP-1", "fallback_scale": 2, "grace": 30, "flat_mouse_during_stream": False})
gvd._configured = False
gvd.ensure_config()
check("config: file loaded once by ensure_config",
      (gvd.CONNECTOR, gvd.FALLBACK_SCALE, gvd.GRACE, gvd.FLAT_MOUSE), ("DP-1", 2.0, 30, False))
check("config: integer scale read as a float", isinstance(gvd.FALLBACK_SCALE, float), True)
check("config: defaults for the other keys",
      (gvd.REMOTE_REFRESH, gvd.SUNSHINE_UNIT, gvd.HELPER, gvd.SUNSHINE_LOG),
      (60.0, "app-dev.lizardbyte.app.Sunshine.service",
       "/usr/local/sbin/gnome-virtual-display-drm",
       os.path.expanduser("~/.config/sunshine/sunshine.log")))
gvd.configure(**TEST_CONFIG)
with open(_cfg, "w") as _f:
    _f.write('connector = "DP-1"\n')
gvd.ensure_config()
check("config: not reloaded once configured", gvd.CONNECTOR, "DP-2")
if _saved_env is None:
    del os.environ["GVD_CONFIG"]
else:
    os.environ["GVD_CONFIG"] = _saved_env

check("config: missing file is empty", gvd.read_config_file(os.path.join(TEST_DIR, "none.toml")), {})
with open(_cfg, "w") as _f:
    _f.write("connector = \n")
for _label, _call in (
        ("invalid TOML", lambda: gvd.read_config_file(_cfg)),
        ("wrong type", lambda: gvd.configure(**{**TEST_CONFIG, "grace": "soon"})),
        ("boolean for a number", lambda: gvd.configure(**{**TEST_CONFIG, "grace": True}))):
    try:
        _call()
        check(f"config: {_label} refused", "accepted", 2)
    except gvd.ConfigError as e:
        check(f"config: {_label} refused", e.code, 2)
gvd.configure(**TEST_CONFIG)

# an unknown key (a file written for another version) is ignored, not fatal
_ignored = gvd.configure(**TEST_CONFIG, future_option=True)
check("config: unknown key ignored", (_ignored, gvd.CONNECTOR), (["future_option"], "DP-2"))

# the example file shipped with the repository loads
_example = os.path.join(HERE, os.pardir, "config.example.toml")
if os.path.exists(_example):
    try:
        gvd.configure(**gvd.read_config_file(_example))
        check("config: config.example.toml loads", gvd.CONNECTOR is not None, True)
    except gvd.ConfigError as e:
        check("config: config.example.toml loads", str(e), "loaded")
gvd.configure(**TEST_CONFIG)

# no connector: clear message and exit code 2 for the commands that need it,
# but `list` works and `reapply` never blocks Sunshine's start
gvd.configure(**{k: v for k, v in TEST_CONFIG.items() if k != "connector"})
try:
    gvd.require_connector()
    check("no connector: refused", "accepted", 2)
except gvd.ConfigError as e:
    check("no connector: refused", e.code, 2)
    check("no connector: the message names the key", "connector" in str(e), True)
_journal.clear()
check("no connector: base exits with 2", gvd.run(["gnome-virtual-display", "base"]), 2)
check("no connector: logged", bool(_journal) and "connector" in _journal[-1], True)
check("no connector: reapply exits with 0", gvd.run(["gnome-virtual-display", "reapply"]), 0)
gvd.cmd_list = lambda: 0
check("no connector: list still runs", gvd.run(["gnome-virtual-display", "list"]), 0)
gvd.cmd_list = _real_list
check("no connector: --help still shows the usage", gvd.run(["gnome-virtual-display", "--help"]), 2)
gvd.configure(**TEST_CONFIG)

# flat mouse switched off: the acceleration profile is never touched
gvd.FLAT_MOUSE = False
_gs["accel-profile"] = "'adaptive'"
gvd.gsettings = _fake_gsettings
gvd.flatten_mouse()
check("flat mouse off: profile untouched", _gs["accel-profile"], "'adaptive'")
check("flat mouse off: nothing to restore", os.path.exists(gvd.MOUSE_FILE), False)
gvd.gsettings = _real_gsettings
gvd.configure(**TEST_CONFIG)

# --- 12. command line -----------------------------------------------------
_calls = []
_saved_cmds = {n: getattr(gvd, n) for n in dir(gvd) if n.startswith("cmd_")}
for _name in _saved_cmds:
    setattr(gvd, _name, (lambda n: (lambda *a, **k: _calls.append((n, a)) or 0))(_name))
gvd.lock = lambda *a, **k: None
for _argv, _want in ((["on"], ("cmd_extend", ())),
                     (["extend"], ("cmd_extend", ())),
                     (["wait"], ("cmd_wait", ("extend",))),
                     (["prep", "remote"], ("cmd_prep", ("remote",))),
                     (["reapply"], ("cmd_reapply", ())),
                     (["boot"], ("cmd_boot", ())),
                     (["fix-session"], ("cmd_fix_session", ())),
                     (["rotate", "left"], ("cmd_rotate", ("left", None))),
                     (["rotate", "left", "HDMI-A-1"], ("cmd_rotate", ("left", "HDMI-A-1"))),
                     (["scale"], ("cmd_scale", ("1.25", "DP-2")))):
    _calls.clear()
    gvd.main(["gnome-virtual-display"] + _argv)
    check(f"cli: {' '.join(_argv)}", _calls, [_want])
for _old in ("seul", "distance", "attente", "amorce", "applique", "echelle", "rotation",
             "repare-session"):
    _usage_hits.clear()
    gvd.usage = lambda: _usage_hits.append(1) or 2
    check(f"cli: old command {_old!r} not accepted", gvd.main(["gnome-virtual-display", _old]), 2)
gvd.usage, gvd.lock = _real_usage, _real_lock
for _name, _fn in _saved_cmds.items():
    setattr(gvd, _name, _fn)

# `rotate` without a connector: the first physical monitor Mutter lists
_real_transform = gvd.apply_transform
_rot = {}
gvd.apply_transform = lambda conn, transform: _rot.update(conn=conn, transform=transform) or True
gvd.state = lambda: (1, [DP2, HDMI, DP3], [])
check("rotate: default is the first physical monitor", gvd.first_physical(), "HDMI-A-1")
gvd.cmd_list = lambda: 0
gvd.cmd_rotate("left")
check("rotate: applied to the first physical monitor", (_rot.get("conn"), _rot.get("transform")),
      ("HDMI-A-1", 1))
gvd.state = lambda: (1, [DP2], [])
try:
    gvd.first_physical()
    check("rotate: no physical monitor refused", "accepted", "SystemExit")
except SystemExit:
    check("rotate: no physical monitor refused", "SystemExit", "SystemExit")
gvd.state, gvd.apply_transform, gvd.cmd_list = _real_state, _real_transform, _real_list


# --- 13. several physical monitors ----------------------------------------
# Left DP-1 1920x1080, middle HDMI-A-1 2560x1440 (primary), right DP-3 portrait.
with_virtual_state()
P_L = (("DP-1", "AAA", "LEFT-24", "1"), [hdmi(1920, 1080)], {})
P_M = (("HDMI-A-1", "BBB", "MID-27", "2"), [hdmi(2560, 1440)], {})
P_R = (("DP-3", "CCC", "RIGHT-24", "3"), [hdmi(1920, 1080)], {})
M3 = [P_L, P_M, P_R, DP2]
G3_L = (0, 300, 1.0, gvd.NORMAL, False, [P_L[0]])
G3_M = (1920, 0, 1.0, gvd.NORMAL, True, [P_M[0]])
G3_R = (4480, 0, 1.0, 1, False, [P_R[0]])  # portrait: 1080 x 1920 logical
_prefs.state = {
    "virtual": {"scale": 1.25, "transform": gvd.NORMAL, "x": None, "y": None},
    "reals": {
        "DP-1": {"x": 0, "y": 300, "scale": 1.0, "transform": 0, "primary": False, "mode": [1920, 1080, 60.0]},
        "HDMI-A-1": {"x": 1920, "y": 0, "scale": 1.0, "transform": 0, "primary": True, "mode": [2560, 1440, 60.0]},
        "DP-3": {"x": 4480, "y": 0, "scale": 1.0, "transform": 1, "primary": False, "mode": [1920, 1080, 60.0]},
    },
}
layout = gvd.build_layout(M3, [G3_L, G3_M, G3_R])
reals3 = [g for g in layout if g[5][0][0] != "DP-2"]
virt3 = next(g for g in layout if g[5][0][0] == "DP-2")
check("3 monitors extend: the three physical monitors kept as they are",
      sorted((g[5][0][0], g[0], g[1], g[3]) for g in reals3),
      [("DP-1", 0, 300, 0), ("DP-3", 4480, 0, 1), ("HDMI-A-1", 1920, 0, 0)])
check("3 monitors extend: virtual right of the rightmost monitor", virt3[0], 4480 + 1080)
check("3 monitors extend: virtual centred on it", virt3[1], int(round((1920 - 1600 / 1.25) / 2)))
check("3 monitors extend: virtual primary, the others not",
      (virt3[4], [g[4] for g in reals3]), (True, [False, False, False]))
rects = [gvd.rect_of(g, M3, list(g[5])) for g in reals3]
vrect3 = (virt3[0], virt3[1], 2400 / 1.25, 1600 / 1.25)
check("3 monitors extend: no overlap", any(gvd.overlap(vrect3, r) for r in rects), False)
check("3 monitors extend: touches the rightmost one", any(gvd.touches(vrect3, r) for r in rects), True)
check("3 monitors extend: no mirror", gvd.mirrored(layout), [])

# During the stream the virtual monitor is primary: the remembered primary
# monitor must not change, so that base gives the role back to it.
G3_V = (5560, 320, 1.25, gvd.NORMAL, True, [("DP-2", "2400x1600@120.000", {})])
G3_M_SEC = (1920, 0, 1.0, gvd.NORMAL, False, [P_M[0]])
_sp_loader = importlib.machinery.SourceFileLoader("gvd_prefs", SCRIPT)
sp = importlib.util.module_from_spec(importlib.util.spec_from_loader("gvd_prefs", _sp_loader))
_sp_loader.exec_module(sp)  # second copy with the real load_prefs, for the prefs file
sp.configure(**TEST_CONFIG)
sp_dir = isolate(sp)
import json as _json
with open(sp.PREFS_FILE, "w") as f:
    _json.dump(_prefs.state, f)
sp.save_prefs(M3, [G3_L, G3_M_SEC, G3_R, G3_V])
with open(sp.PREFS_FILE) as f:
    saved = _json.load(f)
check("3 monitors: primary not learnt during a stream",
      {k: v["primary"] for k, v in saved["reals"].items()},
      {"DP-1": False, "HDMI-A-1": True, "DP-3": False})
sp.save_prefs(M3, [(0, 300, 1.0, 0, True, [P_L[0]]), G3_M_SEC, G3_R])
with open(sp.PREFS_FILE) as f:
    saved = _json.load(f)
check("3 monitors: primary learnt outside streams (Settings)",
      {k: v["primary"] for k, v in saved["reals"].items()},
      {"DP-1": True, "HDMI-A-1": False, "DP-3": False})

layout = gvd.base_layout(M3, [G3_L, G3_M_SEC, G3_R, G3_V])
check("3 monitors base after a stream: primary back on the middle monitor",
      [(g[5][0][0], g[4]) for g in layout], [("DP-1", False), ("HDMI-A-1", True), ("DP-3", False)])
check("3 monitors base after a stream: virtual off", any(g[5][0][0] == "DP-2" for g in layout), False)

layout = gvd.base_layout(M3, [G3_V])
check("3 monitors base after only: all three back",
      sorted((g[5][0][0], g[0], g[1], g[3]) for g in layout),
      [("DP-1", 0, 300, 0), ("DP-3", 4480, 0, 1), ("HDMI-A-1", 1920, 0, 0)])
check("3 monitors base after only: one primary, the middle one",
      [g[5][0][0] for g in layout if g[4]], ["HDMI-A-1"])

layout = gvd.base_layout(M3, [(0, 300, 1.0, 0, True, [P_L[0]]), G3_M_SEC, G3_R])
check("3 monitors base outside a stream: the live primary choice is kept",
      layout is None or [g[5][0][0] for g in layout if g[4]] == ["DP-1"], True)

gvd.snapshot_xml(M3, [G3_V])
root = ET.parse(gvd.MONITORS_XML).getroot()
prim = [lm.findtext("monitor/monitorspec/connector") for lm in root.iter("logicalmonitor")
        if lm.findtext("primary") == "yes"]
check("3 monitors only: monitors.xml has a single primary, the middle one", prim, ["HDMI-A-1"])
check("3 monitors only: monitors.xml never names the virtual monitor",
      "DP-2" in ET.tostring(root, encoding="unicode"), False)

# A monitor whose scale was set while it was off has no remembered mode.
_prefs.state["reals"]["DP-1"] = {"scale": 1.25}
try:
    extras = gvd.extra_reals(M3, [G3_M, G3_R])
    check("monitor remembered without a mode: turned back on at its best mode",
          [(e[5][0][0], e[2]) for e in extras], [("DP-1", 1.25)])
except KeyError as e:
    check("monitor remembered without a mode: turned back on at its best mode", f"KeyError {e}", "no error")
with_virtual_state()

# --- 14. match_client -----------------------------------------------------
_env_saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("SUNSHINE_CLIENT_")}
check("no client variables: no request", gvd.client_request("extend"), None)
os.environ.update(SUNSHINE_CLIENT_WIDTH="1920", SUNSHINE_CLIENT_HEIGHT="1080", SUNSHINE_CLIENT_FPS="120")
check("client request read", gvd.client_request("extend"), (1920, 1080, 120.0))
check("remote keeps remote_refresh", gvd.client_request("remote"), (1920, 1080, 60.0))
fixture = gvd.read_edid(EDID_FILE)
check("EDID offers its own mode", gvd.edid_offers(fixture, 2400, 1600, 120), True)
check("EDID lacks another mode", gvd.edid_offers(fixture, 1920, 1080, 120), False)

_calls = []


def _fake_helper(action, *args):
    _calls.append((action,) + args)
    if args:
        w, rest = args[0].split("x")
        h, r = rest.split("@")
        if int(w) * int(h) * int(r) > 3840 * 2160 * 75:  # over the pixel clock limit
            raise SystemExit("pixel clock over the EDID 1.4 limit")
        with open(EDID_FILE, "wb") as f:
            f.write(make_edid(int(w), int(h), float(r), product="Virtual"))
    return ""


def _fake_state_for(w, h, r):
    vmon = (("DP-2", "LNX", "Virtual", "0"), [mode(w, h, float(r), preferred=True)], {})
    return lambda: (1, [DP3, vmon], [G_DP3])


_real_helper, _real_state2, _real_settle = gvd.helper, gvd.state, gvd.settle
gvd.helper, gvd.settle = _fake_helper, lambda *a, **k: None
gvd.state = _fake_state_for(1920, 1080, 120)
check("new client: EDID written", gvd.match_client_mode("extend"), True)
check("new client: helper asked for the client's mode", _calls, [("on", "1920x1080@120")])
check("new client: native mode follows", gvd.VIRTUAL_NATIVE, (1920, 1080))
_calls.clear()
check("same client again: nothing to do", gvd.match_client_mode("extend"), False)
check("same client again: no hotplug", _calls, [])
os.environ.update(SUNSHINE_CLIENT_WIDTH="3840", SUNSHINE_CLIENT_HEIGHT="2160", SUNSHINE_CLIENT_FPS="120")
gvd.state = _fake_state_for(3840, 2160, 60)
check("4K 120 Hz client: falls back to 60 Hz", gvd.match_client_mode("extend"), True)
check("4K 120 Hz client: 120 refused then 60", [c[1] for c in _calls], ["3840x2160@120", "3840x2160@60"])
_calls.clear()
gvd.MATCH_CLIENT = False
os.environ.update(SUNSHINE_CLIENT_WIDTH="1280", SUNSHINE_CLIENT_HEIGHT="720", SUNSHINE_CLIENT_FPS="60")
check("match_client off: nothing", (gvd.match_client_mode("extend"), _calls), (False, []))
gvd.MATCH_CLIENT = True
gvd.helper, gvd.state, gvd.settle = _real_helper, _real_state2, _real_settle
for k in list(os.environ):
    if k.startswith("SUNSHINE_CLIENT_"):
        del os.environ[k]
os.environ.update(_env_saved)
with open(EDID_FILE, "wb") as _f:
    _f.write(make_edid())
gvd.reload_edid()
check("fixture EDID back", gvd.VIRTUAL_NATIVE, (2400, 1600))

# A remembered scale that does not fit the client's new resolution: Mutter's
# preferred scale for that mode, never a layout Mutter would refuse.
DP2_1080 = (DP2[0], [(  "1920x1080@60.000", 1920, 1080, 60.0, 1.0, [1.0, 2.0], {"is-preferred": True})], {})
gvd.load_prefs = virt_prefs(1.3333333730697632)
layout = gvd.build_layout([DP3, DP2_1080], [G_DP3])
check("new resolution: unsupported remembered scale replaced",
      round(next(g for g in layout if g[5][0][0] == "DP-2")[2], 4), 1.0)
gvd.load_prefs = _prefs_override

# `scale` refuses what Mutter refuses, before remembering it
gvd.state = lambda: (1, [DP3, DP2], [G_DP3])
before_scale = _prefs.state["virtual"]["scale"]
try:
    gvd.cmd_scale("1.5")
    check("scale 1.5 refused on 2400x1600", "accepted", "SystemExit")
except SystemExit as e:
    check("scale 1.5 refused on 2400x1600", "accepted:" in str(e), True)
check("refused scale not remembered", _prefs.state["virtual"]["scale"], before_scale)
gvd.state = _real_state2

# A Quit from Moonlight only logs the undo command: the session is over.
gvd.SUNSHINE_LOG = LOG  # section 11 reconfigured the module
log(f"[2025-01-10 12:00:00.000]: Info: Executing Do Cmd: [{PREP} extend]",
    "[2025-01-10 12:00:01.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:00:01.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:00:01.000]: Info: Mapped 'DP-3' to kmsgrab monitor index 0",
    f"[2025-01-10 12:05:00.000]: Info: Executing Undo Cmd: [{UNDO}]")
check("wrong monitor streamed, session ended by Quit", gvd.captured_wrong_screen(), True)


# --- 15. Mutter's acceptance rules ----------------------------------------
# Mutter refuses a layout whose monitors overlap, do not all touch, or do not
# start at (0, 0) (meta_verify_logical_monitor_config_list). Every layout the
# tool sends goes through fit_layout() first.


def mutter_refuses(layout):
    rects = []
    for g in layout:
        size = gvd.group_size(g)
        rects.append((float(g[0]), float(g[1]), size[0], size[1]))
    errors = []
    for i, a in enumerate(rects):
        if any(gvd.overlap(a, b) for b in rects[i + 1:]):
            errors.append("overlap")
    if len(rects) > 1 and any(not any(gvd.touches(a, b) for j, b in enumerate(rects) if j != i)
                              for i, a in enumerate(rects)):
        errors.append("not adjacent")
    if min(r[0] for r in rects) != 0 or min(r[1] for r in rects) != 0:
        errors.append("offset")
    return errors


LAND = (("DP-1", "AAA", "LAND-24", "1"), [hdmi(1920, 1080)], {})
VIRT = (("DP-2", "LNX", gvd.VIRTUAL_PRODUCT, "0"), [mode(2400, 1600, 120.0, preferred=True)], {})
with_virtual_state(scale=1.25)
_prefs.state["reals"] = {"DP-1": {"x": 0, "y": 0, "scale": 1.0, "transform": 0, "primary": True,
                                  "mode": [1920, 1080, 60.0]}}
raw = gvd.build_layout([LAND, VIRT], [(0, 0, 1.0, 0, True, [LAND[0]])])
check("landscape monitor + taller virtual: raw layout goes above y=0", min(g[1] for g in raw) < 0, True)
fitted = gvd.fit_layout(raw)
check("landscape monitor + taller virtual: accepted by Mutter", mutter_refuses(fitted), [])
check("landscape monitor + taller virtual: virtual still on the right",
      [g[5][0][0] for g in sorted(fitted, key=lambda g: g[0])], ["DP-1", "DP-2"])

m1080 = "1920x1080@60.000"
gap = [(0, 0, 1.0, 0, True, [("DP-1", m1080, {})]), (3840, 0, 1.0, 0, False, [("DP-3", m1080, {})])]
check("middle monitor unplugged (gap): accepted", mutter_refuses(gvd.fit_layout(gap)), [])
left_gone = [(1920, 0, 1.0, 0, True, [("HDMI-1", m1080, {})]), (3840, 0, 1.0, 0, False, [("DP-3", m1080, {})])]
check("left monitor unplugged (offset): accepted", mutter_refuses(gvd.fit_layout(left_gone)), [])
check("left monitor unplugged: order kept",
      [(g[5][0][0], g[0]) for g in gvd.fit_layout(left_gone)], [("HDMI-1", 0), ("DP-3", 1920)])
rotated = [(0, 0, 1.0, 0, True, [("DP-1", m1080, {})]), (1920, 0, 1.0, 0, False, [("DP-3", m1080, {})]),
           (3840, 0, 1.0, 0, False, [("HDMI-1", m1080, {})])]
rotated[0] = (0, 0, 1.0, 0, True, [("DP-1", m1080, {})])
rotated[1] = (1920, 0, 0.75, 0, False, [("DP-3", m1080, {})])  # scale 0.75: 2560 wide now
check("monitor grown by a new scale (overlap): accepted", mutter_refuses(gvd.fit_layout(rotated)), [])
check("monitor grown by a new scale: order kept",
      [g[5][0][0] for g in sorted(gvd.fit_layout(rotated), key=lambda g: g[0])], ["DP-1", "DP-3", "HDMI-1"])
stacked = [(0, 0, 1.0, 0, True, [("DP-1", m1080, {})]), (0, 1080, 1.0, 0, False, [("DP-3", m1080, {})])]
check("stacked monitors (valid): untouched", gvd.fit_layout(stacked), [tuple(g) for g in stacked])
own = [(0, 0, 1.0, 1, True, [("DP-3", m1080, {})]),
       (1080, 320, 1.25, 0, False, [("DP-2", "2400x1600@120.000", {})])]
check("portrait monitor + virtual (valid): untouched", gvd.fit_layout(own), [tuple(g) for g in own])
check("rotation by 180 degrees keeps the size", gvd.group_size((0, 0, 1.0, 2, True, [("DP-1", m1080, {})])),
      (1920.0, 1080.0))

# --- 16. HDMI: kernel and Mutter names ------------------------------------
gvd.configure(**dict(TEST_CONFIG, connector="HDMI-A-1"))
check("HDMI: Mutter name", gvd.CONNECTOR, "HDMI-1")
check("HDMI: kernel name for the helper", gvd.KERNEL_CONNECTOR, "HDMI-A-1")
check("HDMI: virtual recognised in Mutter's state",
      gvd.is_virtual((("HDMI-1", "LNX", "Other", "0"), [], {})), True)
_ran = {}
hp_loader = importlib.machinery.SourceFileLoader("gvd_hdmi", SCRIPT)
hp = importlib.util.module_from_spec(importlib.util.spec_from_loader("gvd_hdmi", hp_loader))
hp_loader.exec_module(hp)  # fresh copy: its helper() is the real one
hp.configure(**dict(TEST_CONFIG, connector="HDMI-A-1"))
isolate(hp)
_real_run = hp.subprocess.run
hp.subprocess.run = lambda cmd, **k: _ran.update(cmd=cmd) or type(
    "R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
try:
    hp.helper("on", "1920x1080@60")
finally:
    hp.subprocess.run = _real_run
check("HDMI: the helper gets the kernel name", _ran["cmd"][3:], ["on", "HDMI-A-1", "1920x1080@60"])
gvd.configure(**TEST_CONFIG)
check("DP: same name on both sides", (gvd.CONNECTOR, gvd.KERNEL_CONNECTOR), ("DP-2", "DP-2"))

# --- 17. sessions are counted ---------------------------------------------
S1 = "[2025-01-10 12:00:00.000]: Info: New streaming session started [active sessions: 1]"
S2 = "[2025-01-10 12:01:00.000]: Info: New streaming session started [active sessions: 2]"
CC = "[2025-01-10 12:00:00.000]: Info: CLIENT CONNECTED"
PT = "[2025-01-10 12:01:30.000]: Info: 192.0.2.7: Ping Timeout"
DC = "[2025-01-10 12:05:00.000]: Info: CLIENT DISCONNECTED"
UN = f"[2025-01-10 12:05:00.000]: Info: Executing Undo Cmd: [{UNDO}]"
check("one session open", gvd.open_sessions("\n".join([S1, CC])), 1)
check("reconnection: old session's timeout comes last, new one still open",
      gvd.open_sessions("\n".join([S1, CC, S2, CC, PT])), 1)
check("both ended", gvd.open_sessions("\n".join([S1, CC, S2, CC, PT, DC])), 0)
check("Quit from Moonlight: undo alone ends it", gvd.open_sessions("\n".join([S1, CC, UN])), 0)
check("client vanished: Ping Timeout ends it", gvd.open_sessions("\n".join([S1, CC, PT])), 0)
check("Sunshine restarted (journal): reset", gvd.open_sessions("\n".join([S1, CC, "Started app.service"])), 0)
_real_journal_fn = gvd.sunshine_journal
gvd.SUNSHINE_LOG = os.path.join(TEST_DIR, "no-such.log")
gvd.sunshine_journal = lambda *a, **k: "\n".join([S1, CC])
check("log file missing: read from the journal", gvd.stream_open(), True)
gvd.sunshine_journal = lambda *a, **k: "\n".join([S1, CC, DC])
check("log file missing: ended session seen in the journal", gvd.stream_open(), False)
gvd.sunshine_journal = _real_journal_fn
gvd.SUNSHINE_LOG = LOG

# --- 18. GNOME Settings are learnt outside streams ------------------------
lp_loader = importlib.machinery.SourceFileLoader("gvd_learn", SCRIPT)
lp = importlib.util.module_from_spec(importlib.util.spec_from_loader("gvd_learn", lp_loader))
lp_loader.exec_module(lp)
lp.configure(**TEST_CONFIG)
lp_dir = isolate(lp)
A = (("DP-1", "AAA", "A", "1"), [hdmi(1920, 1080)], {})
B = (("DP-3", "BBB", "B", "2"), [hdmi(1920, 1080)], {})
lp.save_prefs([A, B, DP2], [(0, 0, 1.0, 0, True, [A[0]]), (1920, 0, 1.0, 0, False, [B[0]])])
lp.save_prefs([A, B, DP2], [(0, 0, 1.0, 1, True, [A[0]]), (1080, 0, 1.0, 0, False, [B[0]])])
learnt = lp.load_prefs()["reals"]
check("Settings: rotation learnt outside a stream", (learnt["DP-1"]["transform"], learnt["DP-3"]["x"]), (1, 1080))
# Mutter drops DP-3 for a moment on its own (KMS capture, hotplug): never learnt
lp.save_prefs([A, B, DP2], [(0, 0, 1.0, 1, True, [A[0]])], learn_disabled=True)
check("monitor dropped for a moment by Mutter: not remembered as off",
      lp.load_prefs()["reals"]["DP-3"].get("enabled"), True)
lp.save_prefs([A, B, DP2], [(0, 0, 1.0, 1, True, [A[0]])])  # reapply / command: never learns it either
check("reapply never learns a monitor as off", lp.load_prefs()["reals"]["DP-3"].get("enabled"), True)
# GNOME Settings switched DP-3 off and kept the change: it wrote monitors.xml itself


def _spec(m):
    c, v, pr, se = m[0]
    return (f"<monitorspec><connector>{c}</connector><vendor>{v}</vendor><product>{pr}</product>"
            f"<serial>{se}</serial></monitorspec>")


with open(lp.MONITORS_XML, "w") as f:
    f.write("<monitors version=\"2\"><configuration><logicalmonitor><x>0</x><y>0</y><scale>1</scale>"
            "<primary>yes</primary><transform><rotation>left</rotation><flipped>no</flipped></transform>"
            f"<monitor>{_spec(A)}<mode><width>1920</width><height>1080</height><rate>60.000</rate></mode>"
            f"</monitor></logicalmonitor><disabled>{_spec(B)}</disabled></configuration></monitors>")
lp.save_prefs([A, B, DP2], [(0, 0, 1.0, 1, True, [A[0]])], learn_disabled=True)
check("Settings: monitor switched off remembered as off", lp.load_prefs()["reals"]["DP-3"]["enabled"], False)
lp.RESTORE = True
lay = lp.base_layout([A, B, DP2], [(0, 0, 1.0, 1, True, [A[0]]),
                                   (1080, 0, 1.25, 0, False, [("DP-2", "2400x1600@120.000", {})])])
check("after a stream: the monitor switched off stays off", [g[5][0][0] for g in lay], ["DP-1"])
lp.RESTORE = False
lp.save_prefs([A, B, DP2], [(0, 0, 1.0, 0, True, [A[0], B[0]])])  # mirror of two physical monitors
check("monitor shown again: enabled again", lp.load_prefs()["reals"]["DP-3"]["enabled"], True)
lp.RESTORE = True
lay = lp.base_layout([A, B, DP2], [(0, 0, 1.0, 0, True, [A[0], B[0]]),
                                   (1920, 0, 1.25, 0, False, [("DP-2", "2400x1600@120.000", {})])])
check("mirror of two physical monitors kept whole", [[c[0] for c in g[5]] for g in lay], [["DP-1", "DP-3"]])
lp.RESTORE = False
lp.snapshot_xml([A, B, DP2], [(0, 0, 1.0, 0, True, [A[0], B[0]])])
root = ET.parse(lp.MONITORS_XML).getroot()
check("monitors.xml: one <monitor> per mirrored monitor",
      [len(lm.findall("monitor")) for lm in root.iter("logicalmonitor")], [2])
# another monitor set (a docked profile) survives
with open(lp.MONITORS_XML) as f:
    xml_text = f.read()
other = ("<configuration><logicalmonitor><x>0</x><y>0</y><scale>1</scale><primary>yes</primary>"
         "<monitor><monitorspec><connector>eDP-1</connector><vendor>X</vendor><product>Y</product>"
         "<serial>Z</serial></monitorspec><mode><width>1920</width><height>1200</height>"
         "<rate>60.000</rate></mode></monitor></logicalmonitor></configuration>")
with open(lp.MONITORS_XML, "w") as f:
    f.write(xml_text.replace("</monitors>", other + "</monitors>"))
lp.snapshot_xml([A, B, DP2], [(0, 0, 1.0, 0, True, [A[0]]), (1920, 0, 1.0, 0, False, [B[0]])])
sets = [sorted(spec.findtext("connector") for spec in c.iter("monitorspec"))
        for c in ET.parse(lp.MONITORS_XML).getroot().findall("configuration")]
check("monitors.xml: other monitor sets kept", sorted(sets), [["DP-1", "DP-3"], ["eDP-1"]])
# during a stream the file holds the physical monitors as remembered (primary included)
lp.save_prefs([A, B, DP2], [(0, 0, 1.0, 0, False, [A[0]]), (1920, 0, 1.0, 0, True, [B[0]])])
lp.snapshot_xml([A, B, DP2], [(0, 0, 1.0, 0, False, [A[0]]), (1920, 0, 1.0, 0, False, [B[0]]),
                              (3840, 0, 1.25, 0, True, [("DP-2", "2400x1600@120.000", {})])])
prim = [lm.findtext("monitor/monitorspec/connector")
        for c in ET.parse(lp.MONITORS_XML).getroot().findall("configuration")
        for lm in c.iter("logicalmonitor") if lm.findtext("primary") == "yes"
        if any(sp.findtext("connector") == "DP-1" for sp in c.iter("monitorspec"))]
check("monitors.xml during a stream: the user's primary, not the first monitor", prim, ["DP-3"])

# `scale` on the virtual monitor between streams: remembered, not applied
lp.write_mode("base")
lp.state = lambda: (1, [DP3, DP2], [G_DP3])
lp.apply_scale = _forbidden
lp.cmd_list = lambda: 0
lp.cmd_scale("1.25")
check("virtual scale between streams: remembered only", lp.load_prefs()["virtual"]["scale"], 1.25)


# --- 19. second review fixes ----------------------------------------------
# a layout Mutter accepts is only shifted, never rearranged
lshape = [(0, 0, 1.0, 0, True, [("DP-1", "1920x1080@60.000", {})]),
          (0, 1480, 1.0, 0, False, [("DP-3", "1920x1080@60.000", {})]),
          (1920, 0, 1.0, 1, False, [("HDMI-1", "2560x1440@60.000", {})])]  # portrait 1440x2560
check("valid layout with a gap between two monitors that both touch a third: untouched",
      gvd.fit_layout(lshape), [tuple(g) for g in lshape])
offset = [(g[0] + 100, g[1] - 50) + tuple(g[2:]) for g in lshape]
check("valid layout with only an offset: shifted back, shape kept",
      [(g[0], g[1]) for g in gvd.fit_layout(offset)], [(0, 0), (0, 1480), (1920, 0)])
# Sunshine's capture names HDMI like the kernel
gvd.configure(**dict(TEST_CONFIG, connector="HDMI-A-1"))
log(f"[2025-01-10 12:00:00.000]: Info: Executing Do Cmd: [{PREP} extend]",
    "[2025-01-10 12:00:01.000]: Info: New streaming session started [active sessions: 1]",
    "[2025-01-10 12:00:01.000]: Info: CLIENT CONNECTED",
    "[2025-01-10 12:00:01.000]: Info: Mapped 'HDMI-A-1' to kmsgrab monitor index 1",
    "[2025-01-10 12:05:00.000]: Info: CLIENT DISCONNECTED")
gvd.SUNSHINE_LOG = LOG
check("HDMI: Sunshine's 'Mapped HDMI-A-1' is the right screen", gvd.captured_wrong_screen(), False)
gvd.configure(**TEST_CONFIG)
gvd.SUNSHINE_LOG = LOG
# the journal covers several Sunshine runs: only the last one counts
J = "\n".join(["Started sun.service", S1, CC, f"[x]: Info: Executing Do Cmd: [{PREP} extend]",
               "Stopped sun.service", "Starting sun.service", "[x]: Info: Sunshine version: 1",
               "Started sun.service"])
check("journal cut at Sunshine's last start", gvd.last_run(J).splitlines()[0], "Started sun.service")
check("journal cut: no session left from the previous run", gvd.open_sessions(gvd.last_run(J)), 0)
# mirrored physical monitors come back as one group after `only`
_prefs.state["reals"] = {
    "DP-1": {"x": 0, "y": 0, "scale": 1.0, "transform": 0, "primary": True, "mode": [1920, 1080, 60.0]},
    "DP-3": {"x": 0, "y": 0, "scale": 1.0, "transform": 0, "primary": False, "mode": [1920, 1080, 60.0]}}
PA = (("DP-1", "AAA", "A", "1"), [hdmi(1920, 1080)], {})
PB = (("DP-3", "BBB", "B", "2"), [hdmi(1920, 1080)], {})
extras = gvd.extra_reals([PA, PB, DP2], [G_VIRT_ALONE])
check("mirror after only: one group with both monitors", [[c[0] for c in e[5]] for e in extras],
      [["DP-1", "DP-3"]])
# a configuration Mutter would refuse is never written to monitors.xml
_prefs.state["reals"] = {
    "DP-1": {"x": 0, "y": 0, "scale": 1.0, "transform": 0, "primary": True, "mode": [1920, 1080, 60.0]},
    "DP-3": {"x": 500, "y": 0, "scale": 1.0, "transform": 0, "primary": False, "mode": [1920, 1080, 60.0]}}
if os.path.exists(gvd.MONITORS_XML):
    os.remove(gvd.MONITORS_XML)
check("overlapping remembered layout: monitors.xml not written",
      (gvd.snapshot_xml([PA, PB, DP2], [G_VIRT_ALONE]), os.path.exists(gvd.MONITORS_XML)), (False, False))
with_virtual_state()

print()
if FAILED:
    print(f"FAILED ({len(FAILED)})")
    for f in FAILED:
        print("  " + f)
    sys.exit(1)
print("ALL OK")
