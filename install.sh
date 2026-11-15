#!/bin/bash
# install.sh: set up the Sunshine virtual display (and optionally the stream shaping) on a GNOME
# Wayland host. Run it as your user from a clone of this repository; it asks for sudo for the
# root parts. Safe to run again: every step overwrites its own files only.
#
#   ./install.sh                          # everything detected; the virtual display follows each client
#   ./install.sh --stream-qos             # + download shaping while streaming (hosts on Wi-Fi)
#   ./install.sh --edid ~/client-panel.bin   # one fixed EDID (the real panel of your client)
#   ./install.sh --uninstall              # remove everything except your config
#
# Options:
#   --connector NAME     free connector to turn into the virtual display (default: the first free
#                        DisplayPort of the GPU that drives the desktop)
#   --driver NAME        kernel driver of that GPU (default: detected from the connector)
#   --edid FILE          fixed EDID (128 or 256 bytes); turns match_client off
#   --mode WxH@HZ        fixed generated EDID for this mode; turns match_client off
#   --stream-qos         also install stream-qos (download shaping during streams, Wi-Fi hosts)
#   --rate RATE          stream-qos total rate for internet TCP during a stream (default 40mbit)
#   --sunshine-unit U    Sunshine user unit (default: detected)
#   --no-gdm-restart     do not allow the watcher to restart GDM after an Xwayland freeze
#   --no-sunshine-config leave sunshine.conf and apps.json alone (otherwise the capture keys are set
#                        and the Moonlight entries added, after a backup)
#   --no-boot-start      do not start Sunshine at boot (otherwise: user lingering on and Sunshine
#                        wanted by default.target, so the login screen can be streamed after a
#                        reboot and a GDM restart does not stop Sunshine)
#   --uninstall          remove units, root helpers, sudo rules and links (config and state are kept)
set -euo pipefail

REPO=$(cd "$(dirname "$0")" && pwd)
BIN="$HOME/.local/bin"
UNITS="$HOME/.config/systemd/user"
CONF_DIR="$HOME/.config/gnome-virtual-display"
ETC=/etc/gnome-virtual-display
SBIN=/usr/local/sbin
SUDOERS=/etc/sudoers.d/gnome-virtual-display

LIB=/usr/local/lib/gnome-virtual-display
CONNECTOR="" MODE="" EDID="" DRIVER="" QOS=0 RATE=40mbit SUNSHINE_UNIT="" GDM=1 UNINSTALL=0 SUNSHINE_CONFIG=1 BOOT_START=1
STATE="$HOME/.local/state/gnome-virtual-display"
# connector = "..." from config.toml, whatever the spacing or quotes
config_connector() {
  python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb")).get("connector") or "")' \
    "$CONF_DIR/config.toml" 2>/dev/null || true
}
while [ $# -gt 0 ]; do
  case "$1" in
    --connector) CONNECTOR=$2; shift 2 ;;
    --mode) MODE=$2; shift 2 ;;
    --edid) EDID=$2; shift 2 ;;
    --driver) DRIVER=$2; shift 2 ;;
    --stream-qos) QOS=1; shift ;;
    --rate) RATE=$2; shift 2 ;;
    --sunshine-unit) SUNSHINE_UNIT=$2; shift 2 ;;
    --no-gdm-restart) GDM=0; shift ;;
    --no-sunshine-config) SUNSHINE_CONFIG=0; shift ;;
    --no-boot-start) BOOT_START=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

detect_sunshine_unit() {
  local u
  for u in app-dev.lizardbyte.app.Sunshine.service sunshine.service; do
    [ "$(systemctl --user show -p LoadState --value "$u" 2>/dev/null)" = loaded ] \
      && { systemctl --user show -p Id --value "$u"; return; }
  done
  echo ""
}

if [ $UNINSTALL = 1 ]; then
  SUNSHINE_UNIT=${SUNSHINE_UNIT:-$(detect_sunshine_unit)}
  say "Stopping and removing user units"
  systemctl --user disable --now gnome-virtual-display-watch.service gnome-virtual-display-session-end.service \
    stream-qos-watch.service 2>/dev/null || true
  rm -f "$UNITS"/gnome-virtual-display-watch.service "$UNITS"/gnome-virtual-display-session-end.service \
    "$UNITS"/stream-qos-watch.service
  [ -n "$SUNSHINE_UNIT" ] && rm -f "$UNITS/$SUNSHINE_UNIT.d/gnome-virtual-display.conf"
  systemctl --user daemon-reload
  rm -f "$BIN"/gnome-virtual-display "$BIN"/make-edid "$BIN"/stream-qos-watch
  say "Removing root files (sudo)"
  conn=$(config_connector)
  [ -n "$conn" ] && sudo "$SBIN/gnome-virtual-display-drm" off "$conn" 2>/dev/null || true
  sudo "$SBIN/stream-qos" off 2>/dev/null || true
  sudo systemctl disable gnome-virtual-display-unplug.service 2>/dev/null || true
  sudo rm -f /etc/systemd/system/gnome-virtual-display-unplug.service "$SUDOERS" \
    "$SBIN/gnome-virtual-display-drm" "$SBIN/stream-qos" "$ETC/drm.conf" "$ETC/stream-qos.conf" "$LIB/make_edid.py"
  sudo rmdir "$LIB" 2>/dev/null || true
  sudo systemctl daemon-reload
  if [ -f "$HOME/.config/sunshine/apps.json" ]; then
    say "Removing the Moonlight entries and output_name from Sunshine's config"
    backup="$STATE/backup/uninstall-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$backup"
    cp -p "$HOME/.config/sunshine/apps.json" "$HOME/.config/sunshine/sunshine.conf" "$backup/" 2>/dev/null || true
    python3 - "$HOME/.config/sunshine" "${conn:-}" <<'EOF' || say "warning: could not edit Sunshine's apps.json/sunshine.conf (backup in $backup)"
import json, os, re, sys
sun, name = sys.argv[1:]
apps = os.path.join(sun, "apps.json")
d = json.load(open(apps))
d["apps"] = [a for a in d.get("apps", []) if "gnome-virtual-display" not in json.dumps(a)]
json.dump(d, open(apps, "w"), indent=2)
conf = os.path.join(sun, "sunshine.conf")
if name and os.path.exists(conf):
    lines = [l for l in open(conf).read().splitlines()
             if not re.match(rf"^\s*output_name\s*=\s*{re.escape(name)}\s*$", l)]
    open(conf, "w").write("\n".join(lines) + "\n")
EOF
  fi
  if grep -qx wants=1 "$STATE/installed" 2>/dev/null && [ -n "$SUNSHINE_UNIT" ]; then
    rm -f "$UNITS/default.target.wants/$SUNSHINE_UNIT"
  fi
  if grep -qx linger=1 "$STATE/installed" 2>/dev/null; then
    sudo loginctl disable-linger "$USER" || true
  fi
  rm -f "$STATE/installed"
  systemctl --user daemon-reload
  systemctl --user restart "$SUNSHINE_UNIT" 2>/dev/null || true
  say "Done. Kept: $CONF_DIR, $STATE, $ETC/edid.bin"
  exit 0
fi

# ---- connector and GPU ----------------------------------------------------------------------
# The desktop GPU is the one with the most connected outputs; its first free DisplayPort (DP
# carries higher pixel clocks than HDMI on most GPUs) becomes the virtual display. On i915 the
# first free HDMI port: a DisplayPort without a sink has no link rate there, i915 keeps only
# 640x480 on it, while an HDMI port takes the EDID (up to 300 MHz with the HDMI vendor block).
card_driver() { basename "$(readlink -f "/sys/class/drm/$1/device/driver")"; }
detect_connector() {
  local best="" best_n=-1 card n c
  for card in /sys/class/drm/card[0-9]; do
    card=$(basename "$card")
    case "$(card_driver "$card")" in nvidia*) continue ;; esac  # no EDID override there
    n=$(cat /sys/class/drm/"$card"-*/status 2>/dev/null | grep -c '^connected$' || true)
    [ "$n" -gt "$best_n" ] && { best=$card; best_n=$n; }
  done
  [ -n "$best" ] || return 1
  local order="DP HDMI-A"
  [ "$(card_driver "$best")" = i915 ] && order="HDMI-A DP"
  for type in $order; do
    for c in $(ls -d /sys/class/drm/"$best"-"$type"-* 2>/dev/null | sort -V); do
      [ "$(cat "$c/status")" = disconnected ] && { basename "$c" | sed "s/^$best-//"; return 0; }
    done
  done
  return 1
}
connector_card() {
  local c
  for c in /sys/class/drm/card[0-9]-"$1"; do [ -e "$c" ] && { basename "$c" | sed "s/-$1\$//"; return 0; }; done
  return 1
}

# ---- checks ---------------------------------------------------------------------------------
[ "$(id -u)" != 0 ] || die "run as your user, not root (sudo is used where needed)"
[ -n "$CONNECTOR" ] || CONNECTOR=$(config_connector)
if [ -z "$CONNECTOR" ]; then
  CONNECTOR=$(detect_connector) || die "no free connector found; pass --connector (see README: Choosing a connector)"
  say "Virtual display connector: $CONNECTOR (first free output of the desktop GPU)"
fi
if [ -z "$DRIVER" ]; then
  card=$(connector_card "$CONNECTOR") || die "connector $CONNECTOR does not exist on this machine"
  DRIVER=$(card_driver "$card")
  say "GPU driver: $DRIVER ($card)"
fi
case "$DRIVER" in nvidia*) die "the NVIDIA proprietary driver has no EDID override in debugfs; use an AMD or Intel output" ;; esac
[[ "$CONNECTOR" =~ ^(DP|HDMI-A|DVI-D|DVI-I)-[1-9][0-9]?$ ]] || die "connector name not supported: $CONNECTOR"
[[ -z "$DRIVER" || "$DRIVER" =~ ^[a-z0-9_]{1,32}$ ]] || die "invalid --driver: $DRIVER"
[[ "$RATE" =~ ^[0-9]{1,5}[kmg]?bit$ ]] || die "invalid --rate: $RATE (example: 40mbit)"
python3 -c 'import gi, tomllib; gi.require_version("Gio", "2.0")' 2>/dev/null \
  || die "needs python3 >= 3.11 with PyGObject (Fedora: python3-gobject, Debian/Ubuntu: python3-gi)"
if grep -qE '\[(integrity|confidentiality)\]' /sys/kernel/security/lockdown 2>/dev/null; then
  die "the kernel is in lockdown mode (usually Secure Boot): it forbids the debugfs EDID override this tool needs. Disable Secure Boot, or use a dummy HDMI/DP plug instead"
fi
[ "${XDG_SESSION_TYPE:-}" = wayland ] || say "warning: not in a Wayland session right now (XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-unset})"
SUNSHINE_UNIT=${SUNSHINE_UNIT:-$(detect_sunshine_unit)}
[ -n "$SUNSHINE_UNIT" ] || die "no Sunshine user unit found; install Sunshine first or pass --sunshine-unit"
OLD_CONNECTOR=$(config_connector)
MATCH=true
[ -z "$EDID$MODE" ] || MATCH=false
if [ -z "$EDID$MODE" ] && [ -f "$CONF_DIR/config.toml" ]; then
  MATCH=$(sed -n 's/^match_client *= *\(true\|false\).*/\1/p' "$CONF_DIR/config.toml"); MATCH=${MATCH:-true}
fi
if [ -n "$EDID" ]; then
  [ -r "$EDID" ] || die "cannot read $EDID"
  size=$(stat -c %s "$EDID")
  [ "$size" = 128 ] || [ "$size" = 256 ] || die "$EDID is $size bytes; an EDID is 128 or 256 bytes"
fi
[ -f "$UNITS/stream-qos-watch.service" ] && QOS=1  # installed before: keep it
if [ $QOS = 1 ]; then
  command -v tc >/dev/null || die "--stream-qos needs tc (iproute2/iproute-tc)"
  modinfo sch_cake >/dev/null 2>&1 || die "--stream-qos needs the sch_cake kernel module"
  modinfo ifb >/dev/null 2>&1 || die "--stream-qos needs the ifb kernel module"
fi

say "Asking for sudo once"
sudo -v
sudo test -d /sys/kernel/debug/dri || die "debugfs is not mounted at /sys/kernel/debug"
sudo sh -c "ls -d /sys/kernel/debug/dri/*/$CONNECTOR" >/dev/null 2>&1 \
  || die "connector $CONNECTOR not found under /sys/kernel/debug/dri (see README: Choosing a connector)"
if [ -n "$OLD_CONNECTOR" ] && [ "$OLD_CONNECTOR" != "$CONNECTOR" ] && [ -x "$SBIN/gnome-virtual-display-drm" ]; then
  say "Releasing the previous connector $OLD_CONNECTOR"
  sudo "$SBIN/gnome-virtual-display-drm" off "$OLD_CONNECTOR" || true
fi

# ---- EDID -----------------------------------------------------------------------------------
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
if [ -z "$EDID$MODE" ] && ! sudo test -f "$ETC/edid.bin"; then
  MODE=1920x1080@60  # first EDID only: with match_client each stream sets the client's mode
fi
if [ -n "$MODE" ]; then
  say "Generating the EDID for $MODE"
  case "$CONNECTOR" in HDMI*) hdmi=--hdmi ;; *) hdmi= ;; esac
  "$REPO/host/make-edid" "$MODE" $hdmi -o "$tmp/edid.bin"
elif [ -n "$EDID" ]; then
  cp "$EDID" "$tmp/edid.bin"
  "$REPO/host/make-edid" --decode "$tmp/edid.bin" >/dev/null || die "$EDID is not a valid EDID"
fi

# ---- root part ------------------------------------------------------------------------------
say "Installing the root helper, EDID and sudo rules"
sudo install -d -o root -g root -m 755 "$ETC"
sudo install -o root -g root -m 755 "$REPO/host/gnome-virtual-display-drm" "$SBIN/gnome-virtual-display-drm"
sudo install -d -o root -g root -m 755 "$LIB"
sudo install -o root -g root -m 644 "$REPO/host/make-edid" "$LIB/make_edid.py"
[ -f "$tmp/edid.bin" ] && sudo install -o root -g root -m 644 "$tmp/edid.bin" "$ETC/edid.bin"
echo "driver = $DRIVER" > "$tmp/drm.conf"
sudo install -o root -g root -m 644 "$tmp/drm.conf" "$ETC/drm.conf"
{
  echo "# gnome-virtual-display: EDID injection on $CONNECTOR, no password (written by install.sh)"
  echo "$USER ALL=(root) NOPASSWD: $SBIN/gnome-virtual-display-drm on $CONNECTOR, $SBIN/gnome-virtual-display-drm off $CONNECTOR"
  echo "# match_client: same, with the client's mode (WIDTHxHEIGHT@HZ, checked by the helper)"
  echo "$USER ALL=(root) NOPASSWD: $SBIN/gnome-virtual-display-drm on $CONNECTOR *"
  if [ $GDM = 1 ]; then
    echo "# Xwayland freeze: the watcher restarts GDM (the login screen can be streamed, the frozen desktop cannot)"
    echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart gdm.service"
  fi
  if [ $QOS = 1 ]; then
    echo "# stream-qos: shape internet downloads during a stream"
    echo "$USER ALL=(root) NOPASSWD: $SBIN/stream-qos on, $SBIN/stream-qos off"
  fi
} > "$tmp/sudoers"
sudo visudo -cqf "$tmp/sudoers" || die "generated sudo rule does not parse"
sudo install -o root -g root -m 440 "$tmp/sudoers" "$SUDOERS"
sed "s/@CONNECTOR@/$CONNECTOR/g" "$REPO/systemd/system/gnome-virtual-display-unplug.service" > "$tmp/unplug.service"
sudo install -o root -g root -m 644 "$tmp/unplug.service" /etc/systemd/system/gnome-virtual-display-unplug.service
sudo systemctl daemon-reload
sudo systemctl enable gnome-virtual-display-unplug.service >/dev/null
if [ $QOS = 1 ]; then
  sudo install -o root -g root -m 755 "$REPO/host/stream-qos" "$SBIN/stream-qos"
  echo "RATE=$RATE" > "$tmp/stream-qos.conf"
  sudo install -o root -g root -m 644 "$tmp/stream-qos.conf" "$ETC/stream-qos.conf"
fi

# ---- user part ------------------------------------------------------------------------------
say "Linking commands into $BIN"
mkdir -p "$BIN" "$UNITS" "$CONF_DIR"
ln -sfn "$REPO/host/gnome-virtual-display" "$BIN/gnome-virtual-display"
ln -sfn "$REPO/host/make-edid" "$BIN/make-edid"
[ $QOS = 1 ] && ln -sfn "$REPO/host/stream-qos-watch" "$BIN/stream-qos-watch"

if [ ! -f "$CONF_DIR/config.toml" ]; then
  say "Writing $CONF_DIR/config.toml"
  sed "s/^connector = .*/connector = \"$CONNECTOR\"/; s/^sunshine_unit = .*/sunshine_unit = \"$SUNSHINE_UNIT\"/;
       s/^match_client = .*/match_client = $MATCH/" "$REPO/config.example.toml" > "$CONF_DIR/config.toml"
else
  sed -i -E "s/^[[:space:]]*connector[[:space:]]*=.*/connector = \"$CONNECTOR\"/" "$CONF_DIR/config.toml"
  [ -z "$EDID$MODE" ] || sed -i -E "s/^[[:space:]]*match_client[[:space:]]*=.*/match_client = false/" "$CONF_DIR/config.toml"
fi

say "Installing user units"
sed "s/@CONNECTOR@/$CONNECTOR/g" "$REPO/systemd/user/gnome-virtual-display-session-end.service" \
  > "$UNITS/gnome-virtual-display-session-end.service"
cp "$REPO/systemd/user/gnome-virtual-display-watch.service" "$UNITS/"
mkdir -p "$UNITS/$SUNSHINE_UNIT.d"
cp "$REPO/systemd/user/sunshine-reapply.conf" "$UNITS/$SUNSHINE_UNIT.d/gnome-virtual-display.conf"
if [ $QOS = 1 ]; then
  sed "s/@SUNSHINE_UNIT@/$SUNSHINE_UNIT/g" "$REPO/systemd/user/stream-qos-watch.service" > "$UNITS/stream-qos-watch.service"
fi
systemctl --user daemon-reload
systemctl --user enable gnome-virtual-display-session-end.service gnome-virtual-display-watch.service >/dev/null
systemctl --user restart gnome-virtual-display-watch.service
systemctl --user start gnome-virtual-display-session-end.service
if [ $QOS = 1 ]; then
  systemctl --user enable stream-qos-watch.service >/dev/null
  systemctl --user restart stream-qos-watch.service
fi

# ---- Sunshine -------------------------------------------------------------------------------
# Plug and play: set the capture keys in sunshine.conf and add the three Moonlight entries to
# apps.json (existing apps and settings are kept; both files are backed up first).
SUN_DIR="$HOME/.config/sunshine"
ASSETS="$SUN_DIR/assets"
mkdir -p "$SUN_DIR" "$ASSETS"
if python3 -c 'import PIL' 2>/dev/null; then
  python3 "$REPO/sunshine/make-icons" "$ASSETS" >/dev/null
fi
if [ $SUNSHINE_CONFIG = 1 ]; then
  backup="$HOME/.local/state/gnome-virtual-display/backup/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$backup"
  for f in sunshine.conf apps.json; do [ -f "$SUN_DIR/$f" ] && cp -p "$SUN_DIR/$f" "$backup/"; done
  say "Configuring Sunshine (backup: $backup)"
  python3 - "$SUN_DIR" "$CONNECTOR" "$REPO/sunshine/apps.example.json" "$ASSETS" "$HOME" <<'EOF'
import json, os, re, sys
sun, connector, example, assets, home = sys.argv[1:]
conf = os.path.join(sun, "sunshine.conf")
lines = open(conf).read().splitlines() if os.path.exists(conf) else []
wanted = {"capture": "kms", "output_name": connector}
for key, value in wanted.items():
    pat = re.compile(rf"^\s*{key}\s*=")
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = f"{key} = {value}"
            break
    else:
        lines.append(f"{key} = {value}")
open(conf, "w").write("\n".join(lines) + "\n")

apps_file = os.path.join(sun, "apps.json")
text = open(example).read().replace("@ASSETS@", assets).replace("@HOME@", home)
ours = json.loads(text)
try:
    current = json.load(open(apps_file))
except (OSError, ValueError):
    current = {"env": {}, "apps": []}
current.setdefault("env", {})
path = current["env"].get("PATH", "$(PATH)")
if "$(HOME)/.local/bin" not in path:
    current["env"]["PATH"] = path + ":$(HOME)/.local/bin"
mine = lambda a: "gnome-virtual-display" in json.dumps(a.get("prep-cmd", []))
have = {a.get("name") for a in current.get("apps", [])}
added = [a for a in ours["apps"] if a["name"] not in have]
if not any(mine(a) for a in current.get("apps", [])):
    current.setdefault("apps", []).extend(added)
else:
    added = []
json.dump(current, open(apps_file, "w"), indent=2)
print("  apps added:", ", ".join(a["name"] for a in added) or "none (already there)")
EOF
  last=$(grep -hoE 'CLIENT (DIS)?CONNECTED' "$SUN_DIR/sunshine.log" 2>/dev/null | tail -1 || true)
  if [ "$last" = "CLIENT CONNECTED" ]; then
    say "A stream is running: restart Sunshine after it to load the new settings"
  else
    systemctl --user restart "$SUNSHINE_UNIT" || true
  fi
fi
# Sunshine before any session: the login screen can be streamed after a reboot, and the watcher's
# GDM restart (Xwayland freeze) does not take Sunshine down with the session.
if [ $BOOT_START = 1 ]; then
  mkdir -p "$STATE"; touch "$STATE/installed"
  if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]; then
    sudo loginctl enable-linger "$USER" && echo linger=1 >> "$STATE/installed"
  fi
  if [ ! -e "$UNITS/default.target.wants/$SUNSHINE_UNIT" ]; then
    systemctl --user add-wants default.target "$SUNSHINE_UNIT" >/dev/null && echo wants=1 >> "$STATE/installed"
  fi
fi
sunshine_bin=$(command -v sunshine || true)
if [ -n "$sunshine_bin" ] && command -v getcap >/dev/null && ! getcap "$(readlink -f "$sunshine_bin")" | grep -q cap_sys_admin; then
  say "warning: KMS capture needs: sudo setcap cap_sys_admin+p $(readlink -f "$sunshine_bin")"
fi

cat <<EOF

Installed. Virtual display on $CONNECTOR ($DRIVER), match_client = $(sed -n 's/^match_client *= *//p' "$CONF_DIR/config.toml").
In Moonlight, pair this host and open "Extend" (your monitors + the virtual one on the right)
or "Virtual only" (the virtual display alone at the client's resolution).
Check:      gnome-virtual-display list
Settings:   $CONF_DIR/config.toml
EOF
