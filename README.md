# sunshine-gnome-virtual-display

Stream a GNOME Wayland desktop to Moonlight on a **virtual monitor** that matches your client, with
no dummy plug. Use the laptop or tablet as an extra screen next to your monitors, or as the only
screen of the host, then get your desk back exactly as it was when the stream ends.

- **Plug and play.** `./install.sh` finds a free output on your GPU, writes the sudo rules, the
  services and the Sunshine settings, and adds three entries to Moonlight. Nothing to edit by hand.
- **The client's resolution, every time.** Before each stream the virtual monitor takes the
  resolution and refresh rate the Moonlight client asked for (2400x1600 at 120 Hz on a laptop,
  2532x1170 on a phone, 3840x2160 at 60 Hz on a TV).
- **Extend with any number of monitors.** The virtual monitor is placed to the right of your
  rightmost monitor, vertically centred, so the pointer crosses on the whole edge. Your monitors
  keep their position, rotation, scale and primary role; every layout is fitted to Mutter's rules
  (no overlap, no gap, origin at 0,0) before it is applied.
- **Back to normal, always.** When the stream ends (even when the client vanishes), the virtual
  monitor is switched off and your monitors get back the layout they had before the stream,
  primary monitor included. Outside streams you change your monitors in GNOME Settings as usual.
- **Built for GNOME's quirks**: login screen on the right monitor, Mutter's 20 s "keep changes?"
  revert, Xwayland freezes, Sunshine staying stuck on an app. See [how it works](docs/how-it-works.md).
- **Smooth on Wi-Fi.** Optional download shaping during streams and a Moonlight launcher that keeps
  the LAN path instead of a VPN. Measured: 42 ms of network latency down to 6 ms. See
  [network](docs/network.md).

## Moonlight entries

| Entry | What you get |
|---|---|
| **Extend** | your monitors + the virtual one on the right; the virtual one is primary (top bar, dock) |
| **Virtual only** | the virtual monitor alone, your physical monitors off; the host desktop is exactly the client's screen |
| **Remote** | the virtual monitor alone at 60 Hz, for streams over the internet (lower the bitrate and frame rate in the client) |

**Extend** is only listed when a physical monitor is plugged into the host: without one there is
nothing to extend. It comes back by itself when a monitor is plugged in (Sunshine is restarted
between streams to load the change).

Each connection takes the resolution and refresh rate of the client that opens it, also right
after a dropped stream: a client that comes back from another screen (a laptop moved from its
monitor to its own panel) gets its new resolution, on the layout kept for the reconnection.

## Requirements

- GNOME on Wayland (tested with GNOME 50).
- **Secure Boot off**, or more precisely the kernel not in lockdown mode: lockdown forbids writing
  to debugfs, and the installer stops with a message when it is on.
- An **AMD or Intel GPU** with one free DisplayPort or HDMI output (nothing plugged in it; on Intel,
  an HDMI one). The EDID
  is injected through the kernel's debugfs, which the NVIDIA proprietary driver does not offer. A
  machine with an NVIDIA card next to an AMD or Intel one works: the virtual monitor goes on the
  AMD/Intel side.
- [Sunshine](https://github.com/LizardByte/Sunshine) installed as a user service, with KMS capture
  allowed (`cap_sys_admin`, done by the Sunshine packages).
- Python 3.11+ with PyGObject, `sudo`, debugfs mounted at `/sys/kernel/debug` (default on Fedora,
  Ubuntu, Arch).
- For the optional shaping: `tc` with the `sch_cake` and `ifb` modules (stock kernels have them).
- Optional: `eu-stack` (elfutils), used to recognise an Xwayland freeze (see
  [how it works](docs/how-it-works.md#xwayland-freeze)).

## Install

```bash
git clone https://github.com/emoubarak/sunshine-gnome-virtual-display.git
cd sunshine-gnome-virtual-display
./install.sh                 # add --stream-qos if the host streams over Wi-Fi
```

Then pair Moonlight with the host and open **Extend**. `gnome-virtual-display list` shows the outputs
and the current layout. To update: `git pull` (the commands are linked from the clone; run
`./install.sh` again when the root helper or the units changed).

What the installer changes, so nothing surprises you:

- a root helper in `/usr/local/sbin`, its EDID and settings in `/etc/gnome-virtual-display`, and
  sudo rules in `/etc/sudoers.d/gnome-virtual-display` (the helper on that one connector, and
  `systemctl restart gdm.service` for the Xwayland freeze recovery, which ends the session;
  `--no-gdm-restart` leaves that rule out);
- user services, plus a system unit that unplugs the virtual monitor before the login screen;
- `capture` and `output_name` in `sunshine.conf` and three entries in `apps.json`, after a backup in
  `~/.local/state/gnome-virtual-display/backup/` (`--no-sunshine-config` to skip);
- Sunshine started at boot (user lingering on, Sunshine wanted by `default.target`), so the login
  screen can be streamed after a reboot (`--no-boot-start` to skip).

Useful options: `--connector DP-3` to pick the output yourself, `--edid panel.bin` to inject the real
EDID of your client's panel (the virtual monitor starts with it; other clients still get their own
resolution unless you set `match_client = false`), `--uninstall`. Full list: `./install.sh --help`.

### Choosing a connector

The installer takes the first free DisplayPort of the GPU that drives your desktop (NVIDIA cards are
skipped), then HDMI if no DisplayPort is free. On Intel (i915) it takes HDMI first: a DisplayPort
with nothing plugged in has no link rate there and only offers 640x480. Any connector with nothing plugged in works, except
ports that only exist when a dock or a USB-C device is attached. Use the kernel's name (`DP-2`,
`HDMI-A-1`); GNOME calls the latter `HDMI-1`, the tool translates. To list them:
`for c in /sys/class/drm/card*-*; do echo "$(basename $c) $(cat $c/status)"; done`.

## Client settings (Moonlight)

- **Mouse**: in Extend, the pointer must be able to leave the streamed screen. Turn off Moonlight's
  "Optimize mouse for remote desktop instead of games" option (the `mouseacceleration` key in
  `Moonlight.conf`, which really selects absolute mouse mode).
- **Same pointer speed as the client** (Linux client, Flatpak):
  `flatpak override --user --env=SDL_MOUSE_RELATIVE_SYSTEM_SCALE=1 com.moonlight_stream.Moonlight`.
  The host turns its own mouse acceleration off during the stream (`flat_mouse_during_stream`).
- **Quit the app when leaving** ("Quit app on host PC after ending stream" / `--quit-after`): the host goes back
  to its normal layout right away instead of after the grace period.
- On a Linux client that also reaches the host through a VPN, start Moonlight with
  [`client/moonlight-lan`](client/moonlight-lan) (see [network](docs/network.md)).
- **Client with several screens** (GNOME client): Moonlight keeps one resolution for every stream
  and shows it on the screen its window is on, so a 2400x1600 setting streamed on a 1920x1080
  monitor gets black bars on the sides. Start Moonlight with `client/moonlight-lan` and add the
  GNOME Shell extension next to it: each stream then takes the resolution and refresh rate of the
  screen Moonlight opens on, and moving the Moonlight window to another screen before a stream
  reopens Moonlight there, with that screen's mode (never during a stream, and only for a Moonlight
  started by `moonlight-lan`). Desktop entries that start a stream directly
  (`moonlight-lan stream HOST APP`) get `--resolution` and `--fps` too, unless they give them.
  `MATCH_SCREEN=0` in `~/.config/gnome-virtual-display/moonlight-lan.conf` turns it off; without
  the extension Moonlight keeps its own settings.

  ```bash
  ln -s "$PWD/client/moonlight-lan" ~/.local/bin/moonlight-lan
  ln -s "$PWD/client/moonlight-screen@sunshine-gnome-virtual-display" ~/.local/share/gnome-shell/extensions/
  # GNOME loads a new extension at login: log out and back in, then
  gnome-extensions enable moonlight-screen@sunshine-gnome-virtual-display
  ```

## Configuration

`~/.config/gnome-virtual-display/config.toml`, written by the installer. The keys and their defaults
are documented in [config.example.toml](config.example.toml). The most useful ones:

| Key | Default | Meaning |
|---|---|---|
| `connector` | detected | output used for the virtual monitor |
| `match_client` | `true` | follow each client's resolution; `false` keeps one fixed EDID |
| `fallback_scale` | `1.25` | scale of the virtual monitor until you choose one |
| `grace` | `90` | seconds a dropped stream keeps the virtual layout for a quick reconnection |

Your physical monitors: change them in GNOME Settings. Outside streams the tool learns what you set
(position, scale, rotation, primary, monitors switched off) and gives it back after each stream. The
virtual monitor's scale is set with a command, used from the next stream (Mutter only accepts scales
that divide the resolution, the command lists them when it refuses one):

```bash
gnome-virtual-display scale 1.25            # virtual monitor
gnome-virtual-display scale 1.0 DP-1        # a physical monitor (same as GNOME Settings)
gnome-virtual-display rotate left DP-1
```

## Commands

`gnome-virtual-display list | extend | only | remote | base | scale N [OUTPUT] | rotate R [OUTPUT] |
off | fix-session | sync-apps`. The Sunshine entries use `prep MODE`, `wait MODE`, `hold` and
`undo`; the user service runs `boot` and `watch`, and Sunshine `reapply` when it starts. Logs: `journalctl -t gnome-virtual-display` and `journalctl -t stream-qos`.

## Uninstall

`./install.sh --uninstall` removes the services, the root helper, the sudo rules and the links, the
three Moonlight entries and `output_name` from Sunshine's config, and turns off the boot start if the
installer turned it on. Your config, the remembered layout and the EDID are kept, and Sunshine's
files are backed up first in `~/.local/state/gnome-virtual-display/backup/`.

## Tested on

Fedora 44, kernel 7.3, GNOME 50 (Mutter 50), Sunshine 2026.914, AMD Radeon 780M with one physical
monitor plus the virtual one, Moonlight 6.2 on a Linux laptop: Extend, Virtual only, Remote, and
client matching from 2400x1600 at 120 Hz to 1920x1080 at 60 Hz and back. Layouts with several
physical monitors, HDMI naming and unplugged monitors are covered by the test suite
(`python3 tests/test_layout.py`), and the layout fitting is checked against a real Mutter running
headless with three virtual monitors (`tests/check_with_headless_mutter.py`, command in its header),
not yet on real multi-monitor hardware. Also run on an Intel HD Graphics 630 laptop (i915, Kaby Lake,
GTX 1060 next to it): the virtual monitor on a free HDMI port at 2400x1600 and 60 Hz, Extend next to
the laptop panel. The client side for several screens (`moonlight-lan` and its GNOME Shell
extension) is checked against a real GNOME Shell 50 running headless with a 2400x1600 and a
1920x1080 screen (`tests/check_moonlight_screen.sh`). Reports from other GPUs, distributions and
setups are welcome.

## Limits

- EDID 1.4 detailed timings stop at a 655.35 MHz pixel clock and 4095 pixels per side: a 3840x2160
  client gets at most 74 Hz, a 2560x1440 client 159 Hz; above that the tool keeps the
  client's resolution at 60 Hz.
- On an HDMI output the EDID declares an HDMI monitor with the HDMI vendor block (otherwise
  drivers cap the port at single-link DVI speed, 165 MHz). The GPU still applies its own HDMI
  limit: 300 MHz on Intel Kaby Lake, so 2400x1600 tops out at 60 Hz there. A mode the output
  drops is remembered (`~/.local/state/gnome-virtual-display/dropped-modes`) and the next streams
  go straight to 60 Hz.
- HDR is not handled: layouts are applied without a color mode, so an HDR monitor falls back to SDR
  while the tool changes the layout.
- `~/.config/monitors.xml` is rewritten for your current set of monitors (configurations of other
  sets, such as a docked laptop, are kept).

## License

MIT
