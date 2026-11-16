#!/bin/bash
# Checks client/moonlight-lan and the moonlight-screen GNOME Shell extension against a real
# GNOME Shell, headless, with two screens (2400x1600@120 and 1920x1080@60), in a private session
# and a temporary HOME that never touch your desktop. Moonlight is a fake (a GTK window with
# Moonlight's app id, started by a fake "flatpak"), a test extension moves the pointer and windows.
#
#   tests/check_moonlight_screen.sh
set -u
REPO=$(cd "$(dirname "$0")/.." && pwd)
UUID=moonlight-screen@sunshine-gnome-virtual-display

if [ "${1:-}" != --inside ]; then
  T=$(mktemp -d)
  # Wayland socket paths are limited to 108 bytes: keep the runtime dir short.
  RT=$(mktemp -d -p "/run/user/$(id -u)" mst.XXXXXX)
  T=$T RT=$RT timeout 150 dbus-run-session -- "$0" --inside
  rc=$?
  rm -rf "$T" "$RT"
  exit $rc
fi

export HOME=$T/home XDG_CONFIG_HOME=$T/home/.config XDG_DATA_HOME=$T/home/.local/share
export XDG_CACHE_HOME=$T/home/.cache XDG_RUNTIME_DIR=$RT GSETTINGS_BACKEND=keyfile
export PATH=$T/bin:$PATH FAKE_LOG=$T/fake.log
unset DISPLAY WAYLAND_DISPLAY XDG_SESSION_TYPE
EXT=$XDG_DATA_HOME/gnome-shell/extensions
ML="$HOME/.var/app/com.moonlight_stream.Moonlight/config/Moonlight Game Streaming Project/Moonlight.conf"
mkdir -p "$EXT/helper@test" "$T/bin" "$(dirname "$ML")"
chmod 700 "$RT"
ln -s "$REPO/client/$UUID" "$EXT/"
printf '[General]\nbitrate=150000\nfps=120\nheight=1600\nwidth=2400\n\n[hosts]\n' > "$ML"
: > "$FAKE_LOG"

cat > "$EXT/helper@test/metadata.json" <<'EOF'
{"uuid": "helper@test", "name": "test helper", "description": "test", "shell-version": ["45", "46", "47", "48", "49", "50"]}
EOF
cat > "$EXT/helper@test/extension.js" <<'EOF'
import Gio from 'gi://Gio';
import Clutter from 'gi://Clutter';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
const IFACE = `<node><interface name="org.test.Helper">
<method name="Pointer"><arg type="i" direction="out" name="x"/><arg type="i" direction="out" name="y"/></method>
<method name="Warp"><arg type="i" direction="in" name="x"/><arg type="i" direction="in" name="y"/></method>
<method name="Move"><arg type="i" direction="in" name="monitor"/><arg type="b" direction="out" name="ok"/></method>
</interface></node>`;
export default class Helper extends Extension {
  enable() { this._d = Gio.DBusExportedObject.wrapJSObject(IFACE, this); this._d.export(Gio.DBus.session, '/org/test/Helper'); }
  disable() { this._d.unexport(); }
  Pointer() { return global.get_pointer().slice(0, 2); }
  Warp(x, y) { Clutter.get_default_backend().get_default_seat().warp_pointer(x, y); }
  Move(i) {
    const w = global.get_window_actors().map(a => a.meta_window)
      .find(w => w.get_wm_class() === 'com.moonlight_stream.Moonlight' && !w.is_fullscreen());
    if (!w) return false;
    w.move_to_monitor(i);
    return true;
  }
}
EOF
cat > "$T/fake_moonlight.py" <<'EOF'
import json, os, sys
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk
conf = os.path.expanduser("~/.var/app/com.moonlight_stream.Moonlight/config/Moonlight Game Streaming Project/Moonlight.conf")
keys = ('width', 'height', 'fps', 'bitrate')
values = dict(l.split('=', 1) for l in open(conf).read().splitlines() if l.split('=')[0] in keys)
with open(os.environ['FAKE_LOG'], 'a') as f:
    f.write(json.dumps({'args': sys.argv[1:], 'conf': values}) + '\n')
app = Gtk.Application(application_id='com.moonlight_stream.Moonlight', flags=Gio.ApplicationFlags.NON_UNIQUE)
def activate(a):
    Gtk.ApplicationWindow(application=a, title='Moonlight', default_width=800, default_height=600).present()
    if os.environ.get('FAKE_STREAM_AFTER'):
        def stream():
            s = Gtk.Window(application=a, title='Moonlight stream')
            s.fullscreen()
            s.present()
        GLib.timeout_add(int(float(os.environ['FAKE_STREAM_AFTER']) * 1000), stream)
app.connect('activate', activate)
app.run([])
EOF
cat > "$T/bin/flatpak" <<EOF
#!/bin/bash
shift 2  # run com.moonlight_stream.Moonlight
exec python3 "$T/fake_moonlight.py" "\$@"
EOF
chmod +x "$T/bin/flatpak"

gsettings set org.gnome.shell enabled-extensions "['$UUID', 'helper@test']"
gsettings set org.gnome.shell disable-user-extensions false
gsettings set org.gnome.shell welcome-dialog-last-shown-version '999'
gnome-shell --headless --wayland-display=wl-mst --virtual-monitor 2400x1600@120 \
  --virtual-monitor 1920x1080@60 > "$T/shell.log" 2>&1 &
SHELL_PID=$!
export WAYLAND_DISPLAY=wl-mst

call() {  # call PATH INTERFACE METHOD [ARGS]
  gdbus call --session --dest org.gnome.Shell --object-path "$1" --method "$2.$3" "${@:4}"
}
H() { call /org/test/Helper org.test.Helper "$@"; }
E() { call /org/gnome/Shell/Extensions/MoonlightScreen org.gnome.Shell.Extensions.MoonlightScreen "$@"; }
FAIL=0
check() {  # check LABEL GOT WANTED
  if [ "$2" = "$3" ]; then echo "ok    $1"; else echo "FAIL  $1: got '$2', wanted '$3'"; FAIL=1; fi
}
warp() {
  local i
  for i in 1 2 3 4 5; do
    H Warp "$1" "$2" > /dev/null
    sleep 0.3
    [ "$(H Pointer)" = "($1, $2)" ] && return 0
  done
}
conf() { grep -E '^(width|height|fps)=' "$ML" | tr '\n' ' '; }
state() { cut -d' ' -f1-3 "$RT/moonlight-lan.screen" 2>/dev/null; }
# "monitor[,fullscreen][,tracked]" for each Moonlight window
windows() {
  E Windows | python3 -c 'import ast, json, sys
ws = json.loads(ast.literal_eval(sys.stdin.read())[0])
print(" ".join(",".join([str(w["monitor"])] + (["fs"] if w["fullscreen"] else []) + (["tracked"] if w["tracked"] else []))
               for w in ws if w["wm_class"] == "com.moonlight_stream.Moonlight"))'
}
quit_fake() { pkill -f "^python3 $T/fake_moonlight.py"; sleep 1; }

for _ in $(seq 60); do E PointerMonitor > /dev/null 2>&1 && break; sleep 0.5; done
check "extension loaded" "$(E PointerMonitor 2>&1)" "(0, 0, 2400, 1600)"

warp 3000 500
check "pointer on the 1920x1080 screen" "$(python3 "$REPO/client/moonlight-screen")" "1920 1080 60 2400 0"

"$REPO/client/moonlight-lan" > /dev/null 2>&1 &
sleep 4
check "plain start: settings" "$(conf)" "fps=60 height=1080 width=1920 "
check "plain start: screen" "$(state)" "gui 2400 0"
check "plain start: window" "$(windows)" "1,tracked"

H Move 0 > /dev/null  # like Super+Shift+arrow: the pointer stays on the other screen
sleep 9
check "moved: Moonlight started again" "$(wc -l < "$FAKE_LOG")" "2"
check "moved: settings" "$(conf)" "fps=120 height=1600 width=2400 "
check "moved: window on the new screen" "$(windows)" "0,tracked"
quit_fake

warp 1000 500
: > "$FAKE_LOG"
FAKE_STREAM_AFTER=2 "$REPO/client/moonlight-lan" > /dev/null 2>&1 &
sleep 4
H Move 1 > /dev/null
sleep 5
check "stream running: not started again" "$(wc -l < "$FAKE_LOG")" "1"
check "stream running: windows" "$(windows)" "1,tracked 0,fs"
quit_fake

warp 3000 500
: > "$FAKE_LOG"
"$REPO/client/moonlight-lan" stream host "Virtual only" > /dev/null 2>&1 &
sleep 3
"$REPO/client/moonlight-lan" stream host Remote --fps 30 --bitrate 10000 > /dev/null 2>&1 &
sleep 3
check "stream: arguments added" "$(sed -n 1p "$FAKE_LOG" | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)["args"]))')" \
  "stream host Virtual only --resolution 1920x1080 --fps 60 --bitrate 150000"
check "stream: given ones kept" "$(sed -n 2p "$FAKE_LOG" | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)["args"]))')" \
  "stream host Remote --fps 30 --bitrate 10000 --resolution 1920x1080"
check "stream: windows left alone" "$(windows)" "1 1"
quit_fake

mkdir -p "$XDG_CONFIG_HOME/gnome-virtual-display"
printf 'MATCH_SCREEN=0\n' > "$XDG_CONFIG_HOME/gnome-virtual-display/moonlight-lan.conf"
warp 3000 500  # the 1920x1080 screen, while the settings say 2400x1600
"$REPO/client/moonlight-lan" > /dev/null 2>&1 &
sleep 4
check "MATCH_SCREEN=0: settings untouched" "$(conf)" "fps=120 height=1600 width=2400 "
check "MATCH_SCREEN=0: no screen file" "$(state)" ""
quit_fake

if grep -q "JS ERROR" "$T/shell.log"; then
  echo "FAIL  JavaScript errors:"; grep -A5 "JS ERROR" "$T/shell.log"; FAIL=1
fi
kill $SHELL_PID
wait $SHELL_PID 2>/dev/null
exit $FAIL
