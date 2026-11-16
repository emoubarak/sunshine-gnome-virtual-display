#!/usr/bin/env python3
"""Tests for the stream lifecycle of gnome-virtual-display, without Sunshine or a display.

The end of a stream (`wait`, `hold`, `undo`), the Moonlight entries kept in Sunshine's apps.json
(`sync_apps`) and the detection of a physical monitor on the host, with a scripted Sunshine log,
a fake clock and a fake sysfs.

    python3 tests/test_session.py
"""
import copy
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "host", "gnome-virtual-display")
sys.dont_write_bytecode = True
loader = importlib.machinery.SourceFileLoader("gvd_session", SCRIPT)
gvd = importlib.util.module_from_spec(importlib.util.spec_from_loader("gvd_session", loader))
loader.exec_module(gvd)


def _forbidden(*a, **k):
    raise RuntimeError("test: call to the real Mutter, helper or system forbidden")


TMP = tempfile.mkdtemp()
gvd.STATE_DIR = TMP
for name in ("MODE_FILE", "LOCK_FILE", "SESSION_FILE", "MOUSE_FILE", "PREFS_FILE", "DROPPED_FILE",
             "XML_WRITTEN", "LEGACY_VIRTUAL", "RELEASE_FILE", "HIDDEN_APPS_FILE", "APPS_WRITTEN"):
    setattr(gvd, name, os.path.join(TMP, name.lower()))
gvd.MONITORS_XML = os.path.join(TMP, "monitors.xml")
gvd.proxy = gvd.helper = gvd.gsettings = gvd.xwayland_frozen = _forbidden
gvd.sunshine_journal = lambda *a, **k: None
gvd.journal = lambda *a, **k: None
gvd.configure(connector="DP-2", edid="/nonexistent", grace=90,
              sunshine_apps=os.path.join(TMP, "apps.json"), sunshine_log=os.path.join(TMP, "x.log"))

FAILED = []


def check(label, got, wanted):
    if got == wanted:
        print(f"ok    {label}")
    else:
        print(f"FAIL  {label}: got {got!r}, wanted {wanted!r}")
        FAILED.append(label)


def line(t, text):
    return f"[2026-11-16 14:{t // 60:02d}:{t % 60:02d}.000]: Info: {text}"


CONNECTED = "CLIENT CONNECTED"
DISCONNECTED = "CLIENT DISCONNECTED"
STARTED = "New streaming session started [active sessions: 1]"
DO = "Executing Do Cmd: [gnome-virtual-display prep only]"
UNDO = "Executing Undo Cmd: [gnome-virtual-display undo]"


# --- log reading -----------------------------------------------------------------------------

stream = "\n".join([line(1, DO), line(2, STARTED), line(2, CONNECTED)])
check("open_sessions: stream running", gvd.open_sessions(stream), 1)
dropped = stream + "\n" + line(30, DISCONNECTED)
check("open_sessions: stream dropped", gvd.open_sessions(dropped), 0)
check("ended_in_log: end after the session", gvd.ended_in_log(dropped), True)
check("ended_in_log: session still running", gvd.ended_in_log(stream), False)
freed = dropped + "\n" + line(31, "App exited with code [0]") + "\n" + line(31, UNDO)
check("open_sessions: app freed by hold", gvd.open_sessions(freed), 0)
saved_tail = gvd.log_tail
gvd.log_tail = lambda *a, **k: freed
check("app_engaged: false once Sunshine ran the undo", gvd.app_engaged(), False)
gvd.log_tail = lambda *a, **k: dropped
check("app_engaged: true after a drop, before the release", gvd.app_engaged(), True)
gvd.log_tail = saved_tail


# --- undo ------------------------------------------------------------------------------------

calls = []
real_base = gvd.cmd_base
gvd.cmd_base = lambda: calls.append("base") or 0
token = gvd.mark_session()
gvd.write_release(token, gvd.time.time() + 60)
calls.clear()
gvd.cmd_undo()
check("undo: released app, grace running -> layout kept", calls, [])
gvd.write_release(token, gvd.time.time() - 1)
calls.clear()
gvd.cmd_undo()
check("undo: grace over -> base", calls, ["base"])
gvd.write_release("1.0", gvd.time.time() + 60)
calls.clear()
gvd.cmd_undo()
check("undo: release of an older session -> base", calls, ["base"])
gvd.clear_release()
calls.clear()
gvd.cmd_undo()
check("undo: no release (the client quit) -> base", calls, ["base"])


# --- wait ------------------------------------------------------------------------------------

class Clock:
    """Fake time: each sleep advances it."""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s
        script.step(self.now)

    def monotonic(self):
        return self.now


class Script:
    """Sunshine's log as a function of time: [(from_time, tail)]."""

    def __init__(self, steps, token_changes_at=None):
        self.steps, self.token_changes_at, self.t = steps, token_changes_at, 0

    def step(self, now):
        self.t = now - clock_start
        if self.token_changes_at is not None and self.t >= self.token_changes_at:
            gvd.mark_session()

    def tail(self, *a, **k):
        current = self.steps[0][1]
        for start, text in self.steps:
            if self.t >= start:
                current = text
        return current


real = {n: getattr(gvd, n) for n in ("time", "log_tail", "cmd_only", "hold_running",
                                     "restart_sunshine", "stream_open")}


def run_wait(steps, hold=True, token_changes_at=None):
    global script, clock_start
    clock = Clock()
    clock_start = clock.now
    script = Script(steps, token_changes_at)
    events = []
    gvd.time = clock
    gvd.log_tail = script.tail
    gvd.stream_open = lambda: gvd.open_sessions(script.tail()) > 0
    gvd.cmd_only = lambda: None
    gvd.hold_running = lambda: hold
    gvd.restart_sunshine = lambda: events.append(("restart", round(script.t)))
    gvd.cmd_base = lambda: events.append(("base", round(script.t))) or 0
    real_write = gvd.write_release
    gvd.write_release = lambda tok, deadline: (events.append(("release", round(script.t))),
                                               real_write(tok, deadline))
    gvd.clear_release()
    try:
        gvd.cmd_wait("only")
    finally:
        gvd.write_release = real_write
        for n, v in real.items():
            setattr(gvd, n, v)
    return events


before = line(0, "Executing Undo Cmd: [gnome-virtual-display undo]")  # end of an older stream
running = "\n".join([before, line(1, DO), line(3, STARTED), line(3, CONNECTED)])
ended = running + "\n" + line(20, DISCONNECTED)
ended_freed = ended + "\n" + line(21, UNDO)
check("wait: dropped stream -> released after RELEASE_DELAY, base after the grace period",
      run_wait([(0, before + "\n" + line(1, DO)), (3, running), (20, ended), (28, ended_freed)]),
      [("release", 26), ("base", 110)])
late_quit = ended + "\n" + line(24, "Executing Undo Cmd: [gnome-virtual-display undo]")
check("wait: a Quit 4 s after the end of the stream -> base, no release",
      run_wait([(0, before + "\n" + line(1, DO)), (3, running), (20, ended), (24, late_quit)]),
      [("base", 24)])
quit_log = running + "\n" + line(20, "Executing Undo Cmd: [gnome-virtual-display undo]")
check("wait: the client quit -> base at once",
      run_wait([(0, before + "\n" + line(1, DO)), (3, running), (20, quit_log)]), [("base", 20)])
check("wait: entries of an older format -> base and restart after the grace period",
      run_wait([(0, before + "\n" + line(1, DO)), (3, running), (20, ended)], hold=False),
      [("base", 110), ("restart", 110)])
check("wait: the client never connects -> no release",
      run_wait([(0, before + "\n" + line(1, DO))])[:1], [("base", 93)])
check("wait: a newer stream took over -> nothing touched",
      run_wait([(0, before + "\n" + line(1, DO)), (3, running), (20, ended), (28, ended_freed)],
               token_changes_at=30), [("release", 26)])
reconnected = ended + "\n" + line(27, STARTED) + "\n" + line(27, CONNECTED)
check("wait: back before Sunshine freed the app -> release withdrawn, released again later",
      run_wait([(0, before + "\n" + line(1, DO)), (3, running), (20, ended), (27, reconnected),
                (200, reconnected + "\n" + line(200, DISCONNECTED))], hold=True)[:2],
      [("release", 26), ("release", 206)])
quick = ended + "\n" + line(22, STARTED) + "\n" + line(22, CONNECTED)
check("wait: back within RELEASE_DELAY (resume) -> never released",
      run_wait([(0, before + "\n" + line(1, DO)), (3, running), (20, ended), (22, quick),
                (300, quick + "\n" + line(300, UNDO))], hold=True), [("base", 300)])
gvd.cmd_base = real_base


# --- hold ------------------------------------------------------------------------------------

class Stop(Exception):
    pass


def run_hold(tail, release_for_current, limit=10):
    n = {"sleeps": 0}

    class T:
        @staticmethod
        def sleep(_s):
            n["sleeps"] += 1
            if n["sleeps"] > limit:
                raise Stop

        @staticmethod
        def time():
            return 0.0

    tok = gvd.mark_session()
    gvd.write_release(tok if release_for_current else "1.0", 1e12)
    gvd.time, gvd.log_tail = T, lambda *a, **k: tail
    try:
        return gvd.cmd_hold()
    except Stop:
        return "still running"
    finally:
        gvd.time, gvd.log_tail = real["time"], real["log_tail"]


check("hold: released, no session open -> exits", run_hold(ended, True), 0)
check("hold: released but a session is open -> keeps the app", run_hold(running, True),
      "still running")
check("hold: release of an older session -> keeps the app", run_hold(ended, False), "still running")
tok = gvd.mark_session()
gvd.write_release(tok, 1e12)
try:
    gvd.hold_stopped()
except SystemExit:
    pass
check("hold stopped by Sunshine (Quit): its release withdrawn", gvd.read_release(), None)
gvd.write_release("1.0", 1e12)
try:
    gvd.hold_stopped()
except SystemExit:
    pass
check("hold stopped by Sunshine: an older release left alone", gvd.read_release(), ("1.0", 1e12))
gvd.clear_release()

hold_line = line(1, "Executing: [gnome-virtual-display hold] in [\"/home/u\"]")
with_hold = "\n".join([line(1, DO), hold_line, line(3, STARTED), line(3, CONNECTED), line(20, DISCONNECTED)])
gvd.log_tail = lambda *a, **k: with_hold
gvd.hold_running = lambda: False
check("app_engaged: hold gone -> freed (Sunshine has not logged it yet)", gvd.app_engaged(), False)
gvd.hold_running = lambda: True
check("app_engaged: hold running -> engaged", gvd.app_engaged(), True)
gvd.log_tail, gvd.hold_running = real["log_tail"], real["hold_running"]


# --- apps.json -------------------------------------------------------------------------------

def old_entry(name, mode):
    return {"name": name, "image-path": f"/icons/{mode}.png", "working-dir": "/home/u",
            "prep-cmd": [{"do": f"gnome-virtual-display prep {mode}",
                          "undo": "gnome-virtual-display base"}],
            "detached": [f"gnome-virtual-display wait {mode}"]}


desktop = {"name": "Desktop", "image-path": "desktop.png"}
foreign = {"name": "Steam", "cmd": "steam -bigpicture",
           "prep-cmd": [{"do": "gnome-virtual-display prep extend", "undo": "x"}]}
apps = {"env": {"PATH": "$(PATH):$(HOME)/.local/bin"},
        "apps": [old_entry("Extend", "extend"), old_entry("Surface seul", "only"), desktop,
                 old_entry("À distance 60 i/s", "remote"), foreign]}
with open(gvd.SUNSHINE_APPS, "w") as f:
    json.dump(apps, f)

check("sync_apps: dry run sees the change", gvd.sync_apps(write=False, physical=True), True)
check("sync_apps: dry run writes nothing", json.load(open(gvd.SUNSHINE_APPS)), apps)
check("sync_apps: old format migrated", gvd.sync_apps(physical=True), True)
now = json.load(open(gvd.SUNSHINE_APPS))["apps"]
only = now[1]
check("migrated: name and image kept", (only["name"], only["image-path"]),
      ("Surface seul", "/icons/only.png"))
check("migrated: hold is the app's command", (only["cmd"], only["auto-detach"]),
      ("gnome-virtual-display hold", False))
check("migrated: wait detached, undo", (only["detached"], only["prep-cmd"][0]["undo"]),
      (["gnome-virtual-display wait only"], "gnome-virtual-display undo"))
check("migrated: other apps untouched", (now[2], now[4]), (desktop, foreign))
check("sync_apps: idempotent", gvd.sync_apps(physical=True), False)

check("no physical monitor: Extend hidden", gvd.sync_apps(physical=False), True)
names = [a["name"] for a in json.load(open(gvd.SUNSHINE_APPS))["apps"]]
check("no physical monitor: the other entries stay", names,
      ["Surface seul", "Desktop", "À distance 60 i/s", "Steam"])
check("no physical monitor: idempotent", gvd.sync_apps(physical=False), False)
check("physical monitor back: Extend restored", gvd.sync_apps(physical=True), True)
restored = json.load(open(gvd.SUNSHINE_APPS))["apps"]
check("physical monitor back: same place, current format",
      (restored[0]["name"], restored[0]["cmd"], [a["name"] for a in restored][1]),
      ("Extend", "gnome-virtual-display hold", "Surface seul"))
check("physical monitor back: nothing left aside", gvd.load_hidden(), [])

with open(gvd.SUNSHINE_APPS, "w") as f:
    json.dump(apps, f)
os.unlink(gvd.HIDDEN_APPS_FILE)
gvd.sync_apps(physical=False)
hidden = gvd.load_hidden()
check("old-format Extend set aside in the current format",
      (len(hidden), hidden[0]["index"], hidden[0]["app"]["cmd"]),
      (1, 0, "gnome-virtual-display hold"))


# Extend written again while it was set aside: a single Extend comes back.
with open(gvd.SUNSHINE_APPS, "w") as f:
    json.dump(apps, f)
os.unlink(gvd.HIDDEN_APPS_FILE)
gvd.sync_apps(physical=False)
with open(gvd.SUNSHINE_APPS) as f:
    again = json.load(f)
again["apps"].insert(0, old_entry("Extend", "extend"))
with open(gvd.SUNSHINE_APPS, "w") as f:
    json.dump(again, f)
gvd.sync_apps(physical=False)
check("Extend set aside twice: kept once", len(gvd.load_hidden()), 1)
gvd.sync_apps(physical=True)
check("Extend set aside twice: restored once",
      [a["name"] for a in json.load(open(gvd.SUNSHINE_APPS))["apps"]].count("Extend"), 1)

gvd.MANAGE_APPS = False
check("manage_sunshine_apps = false: apps.json left alone", gvd.sync_apps(physical=False), False)
gvd.MANAGE_APPS = True

real_started = gvd.sunshine_started_at
mtime = os.path.getmtime(gvd.SUNSHINE_APPS)
gvd.sunshine_started_at = lambda: mtime - 30
check("apps_stale: written by sync_apps after Sunshine started", gvd.apps_stale(), True)
gvd.sunshine_started_at = lambda: mtime + 30
check("apps_stale: Sunshine started after it", gvd.apps_stale(), False)
gvd.sunshine_started_at = lambda: mtime - 30
os.utime(gvd.SUNSHINE_APPS, (mtime + 5, mtime + 5))  # written since by someone else (web UI)
check("apps_stale: written by Sunshine's web UI", gvd.apps_stale(), False)
gvd.sunshine_started_at = real_started


# --- physical monitors -----------------------------------------------------------------------

def fake_sysfs(connectors, drivers):
    root = tempfile.mkdtemp()
    for card, driver in drivers.items():
        os.makedirs(os.path.join(root, "drivers", driver), exist_ok=True)
        os.makedirs(os.path.join(root, card, "device"))
        os.symlink(os.path.join(root, "drivers", driver), os.path.join(root, card, "device", "driver"))
    for name, status in connectors.items():
        os.makedirs(os.path.join(root, name))
        with open(os.path.join(root, name, "status"), "w") as f:
            f.write(status + "\n")
    return root


minisforum = {"card0-DP-2": "connected", "card0-DP-3": "disconnected",
              "card0-Writeback-1": "unknown", "card1-DP-9": "disconnected"}
drivers = {"card0": "amdgpu", "card1": "nvidia"}
gvd.DRM_CONF = os.path.join(TMP, "no-drm.conf")
gvd.DRM_SYSFS = fake_sysfs(minisforum, drivers)
check("host: only the virtual connector -> no physical monitor", gvd.physical_connected(), False)
gvd.DRM_SYSFS = fake_sysfs(dict(minisforum, **{"card0-DP-3": "connected"}), drivers)
check("host: a monitor on the same GPU", gvd.physical_connected(), True)
gvd.DRM_SYSFS = fake_sysfs(dict(minisforum, **{"card1-DP-9": "connected"}), drivers)
check("host: a monitor on the other GPU", gvd.physical_connected(), True)
same_name = dict(minisforum, **{"card1-DP-2": "connected"})
gvd.DRM_SYSFS = fake_sysfs(same_name, drivers)
with open(os.path.join(TMP, "drm.conf"), "w") as f:
    f.write("driver = amdgpu\n")
gvd.DRM_CONF = os.path.join(TMP, "drm.conf")
check("host: same connector name on the other GPU (drm.conf) -> a monitor",
      gvd.physical_connected(), True)

print()
print("FAILED: " + ", ".join(FAILED) if FAILED else "ALL OK")
sys.exit(1 if FAILED else 0)
