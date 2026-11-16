// Moonlight screen: keeps the stream at the resolution of the screen Moonlight is shown on.
//
// Moonlight reads its resolution once, at startup, and streams on the screen its window is on.
// On a client with several screens (a 3:2 laptop panel and a 16:9 monitor), a stream started on
// the other screen gets black bars. This extension works with client/moonlight-lan:
//   - PointerMonitor() over D-Bus: the screen under the pointer, where Mutter opens a new window.
//     moonlight-lan gives Moonlight that screen's mode before starting it, and writes what it did
//     in $XDG_RUNTIME_DIR/moonlight-lan.screen: "gui CONNECTOR PID LAUNCHER" (PID: its own,
//     which `flatpak run` keeps, so Moonlight's windows are its descendants).
//   - The main window of that start is kept on that screen (found by its connector: plugging or
//     unplugging another screen moves the others, it is not a move). When the window is moved
//     to another screen (drag, Super+Shift+arrow), or its screen is unplugged, and no stream is
//     running, Moonlight is closed and started again through moonlight-lan with
//     MOONLIGHT_SCREEN="X,Y" (the new screen's position), and its new window put on that screen.
// Windows(): debug list of the windows. Logs: journalctl -b /usr/bin/gnome-shell | grep moonlight

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const APP_ID = 'com.moonlight_stream.Moonlight';
const DBUS_PATH = '/org/gnome/Shell/Extensions/MoonlightScreen';
const IFACE = `<node>
  <interface name="org.gnome.Shell.Extensions.MoonlightScreen">
    <method name="PointerMonitor">
      <arg type="i" direction="out" name="x"/>
      <arg type="i" direction="out" name="y"/>
      <arg type="i" direction="out" name="width"/>
      <arg type="i" direction="out" name="height"/>
    </method>
    <method name="Windows">
      <arg type="s" direction="out" name="json"/>
    </method>
  </interface>
</node>`;

const SETTLE_MS = 700;       // a move is over once the window stays put this long
const MAP_GRACE_MS = 2000;   // placement right after the window shows is not a move
const EXIT_WAIT_MS = 8000;   // Moonlight stops its host polling before it quits

function log(message) {
    console.log(`moonlight-screen: ${message}`);
}

function isMoonlight(win) {
    return win.get_window_type() === Meta.WindowType.NORMAL &&
        (win.get_sandboxed_app_id?.() === APP_ID || win.get_wm_class() === APP_ID);
}

// The stream window: "<host> - Moonlight" (session.cpp), fullscreen or not. The main window
// ("Moonlight", fullscreen too with that UI setting) is hidden while it is open.
function isStream(win) {
    return (win.get_title() ?? '').endsWith(' - Moonlight');
}

function moonlightWindows() {
    return global.get_window_actors().map(actor => actor.meta_window)
        .filter(win => win && isMoonlight(win));
}

// Index of the screen on that connector, -1 when it is not there (unplugged, switched off).
function monitorOf(connector) {
    return global.backend.get_monitor_manager().get_monitor_for_connector(connector);
}

function parentPid(pid) {
    try {
        const [, bytes] = GLib.file_get_contents(`/proc/${pid}/stat`);
        const stat = new TextDecoder().decode(bytes);
        return parseInt(stat.slice(stat.lastIndexOf(')') + 2).split(' ')[1]);
    } catch {
        return 0;
    }
}

function descendsFrom(pid, ancestor) {
    for (let i = 0; i < 32 && pid > 1; i++, pid = parentPid(pid)) {
        if (pid === ancestor)
            return true;
    }
    return false;
}

function readState() {
    const path = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'moonlight-lan.screen']);
    try {
        const [, bytes] = GLib.file_get_contents(path);
        const [kind, connector, pid, ...launcher] = new TextDecoder().decode(bytes).trim().split(' ');
        return {kind, connector, pid: parseInt(pid), launcher: launcher.join(' ')};
    } catch {
        return null;
    }
}

export default class MoonlightScreenExtension extends Extension {
    enable() {
        this._windows = new Map();
        this._shown = new Map();
        this._sources = new Set();
        this._grabbing = false;
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, DBUS_PATH);
        const display = global.display;
        this._displaySignals = [
            display.connect('window-created', (_d, win) => this._onCreated(win)),
            display.connect('grab-op-begin', () => {
                this._grabbing = true;
            }),
            display.connect('grab-op-end', (_d, win) => {
                this._grabbing = false;
                if (win && this._windows.has(win))
                    this._schedule(win);
            }),
        ];
        for (const win of moonlightWindows())
            this._track(win, true);
    }

    disable() {
        for (const id of this._displaySignals)
            global.display.disconnect(id);
        for (const [win, ids] of this._shown)
            ids.forEach(id => win.disconnect(id));
        this._shown.clear();
        for (const win of [...this._windows.keys()])
            this._untrack(win);
        for (const id of this._sources)
            GLib.source_remove(id);
        this._sources.clear();
        this._dbus.unexport();
        this._dbus = null;
    }

    PointerMonitor() {
        const r = global.display.get_monitor_geometry(global.display.get_current_monitor());
        return [r.x, r.y, r.width, r.height];
    }

    Windows() {
        return JSON.stringify(global.get_window_actors().map(actor => {
            const win = actor.meta_window;
            return {
                title: win.get_title(), wm_class: win.get_wm_class(),
                sandboxed_app_id: win.get_sandboxed_app_id?.() ?? null, pid: win.get_pid(),
                type: win.get_window_type(), monitor: win.get_monitor(),
                fullscreen: win.is_fullscreen(), tracked: this._windows.has(win),
            };
        }));
    }

    _later(ms, callback) {
        const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._sources.delete(id);
            callback();
            return GLib.SOURCE_REMOVE;
        });
        this._sources.add(id);
        return id;
    }

    _onCreated(win) {
        // Shown: Mutter has placed it, and a Wayland window has its app id by then.
        const done = () => {
            for (const id of this._shown.get(win) ?? [])
                win.disconnect(id);
            this._shown.delete(win);
        };
        this._shown.set(win, [
            win.connect('shown', () => {
                done();
                this._track(win, false);
            }),
            win.connect('unmanaged', done),
        ]);
    }

    _track(win, existing) {
        if (this._windows.has(win) || !isMoonlight(win) || isStream(win))
            return;
        const state = readState();
        // Only the windows of a plain start by moonlight-lan.
        if (!state || state.kind !== 'gui' || !descendsFrom(win.get_pid(), state.pid))
            return;
        const rec = {state, shownAt: GLib.get_monotonic_time(), relaunched: false, timeout: 0};
        rec.signals = [
            win.connect('position-changed', () => this._schedule(win)),
            win.connect('unmanaged', () => this._untrack(win)),
        ];
        this._windows.set(win, rec);
        const target = monitorOf(state.connector);
        if (existing || win.get_monitor() === target)
            return;
        // Its settings are for that screen: a new window opens under the pointer, possibly on
        // another one (start after Super+Shift+arrow, main window shown again after a stream).
        // That screen gone: like a move.
        if (target >= 0)
            win.move_to_monitor(target);
        else
            this._schedule(win);
    }

    _untrack(win) {
        const rec = this._windows.get(win);
        if (!rec)
            return;
        for (const id of rec.signals)
            win.disconnect(id);
        if (rec.timeout) {
            GLib.source_remove(rec.timeout);
            this._sources.delete(rec.timeout);
        }
        this._windows.delete(win);
    }

    _schedule(win) {
        const rec = this._windows.get(win);
        if (!rec || rec.relaunched)
            return;
        if (rec.timeout) {
            GLib.source_remove(rec.timeout);
            this._sources.delete(rec.timeout);
        }
        rec.timeout = this._later(SETTLE_MS, () => {
            rec.timeout = 0;
            this._check(win);
        });
    }

    _check(win) {
        const rec = this._windows.get(win);
        if (!rec || rec.relaunched || this._grabbing)
            return;
        if ((GLib.get_monotonic_time() - rec.shownAt) / 1000 < MAP_GRACE_MS) {
            this._schedule(win);
            return;
        }
        const windows = moonlightWindows();
        if (windows.length !== 1 || windows[0] !== win || windows.some(isStream))
            return;  // a stream is running, or another Moonlight is open
        if (win.get_monitor() === monitorOf(rec.state.connector))
            return;
        this._relaunch(win, rec, global.display.get_monitor_geometry(win.get_monitor()));
    }

    _relaunch(win, rec, r) {
        rec.relaunched = true;
        const launcher = rec.state.launcher;
        if (!launcher || !GLib.file_test(launcher, GLib.FileTest.IS_EXECUTABLE)) {
            log(`launcher "${launcher}" missing, Moonlight left as is`);
            return;
        }
        const pid = win.get_pid();
        log(`Moonlight moved to the screen at ${r.x},${r.y} (${r.width}x${r.height}): reopening it there`);
        win.delete(global.get_current_time());
        const start = GLib.get_monotonic_time();
        const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 100, () => {
            if (pid > 0 && GLib.file_test(`/proc/${pid}`, GLib.FileTest.EXISTS)) {
                if ((GLib.get_monotonic_time() - start) / 1000 < EXIT_WAIT_MS)
                    return GLib.SOURCE_CONTINUE;
                log('Moonlight did not quit, not started again');
            } else {
                this._launch(launcher, r);
            }
            this._sources.delete(id);
            return GLib.SOURCE_REMOVE;
        });
        this._sources.add(id);
    }

    _launch(launcher, r) {
        try {
            const context = global.create_app_launch_context(0, -1);
            context.setenv('MOONLIGHT_SCREEN', `${r.x},${r.y}`);
            Gio.AppInfo.create_from_commandline(GLib.shell_quote(launcher), 'Moonlight',
                Gio.AppInfoCreateFlags.NONE).launch([], context);
        } catch (e) {
            log(`could not start ${launcher}: ${e.message}`);
        }
    }
}
