# How it works

## A monitor that is not there

A GPU output with nothing plugged in can be made to look connected through the kernel's DRM debugfs:

```
/sys/kernel/debug/dri/<card>/<connector>/edid_override    the EDID the "monitor" reports
/sys/kernel/debug/dri/<card>/<connector>/force            on: connected, whatever the cable says
/sys/kernel/debug/dri/<card>/<connector>/trigger_hotplug  tell the compositor something changed
```

`gnome-virtual-display-drm` (root, called through a narrow sudo rule) writes these three files,
then forces the connector once more through sysfs (`/sys/class/drm/card<N>-<connector>/status`,
`detect` then `on`). The debugfs `force` file does not probe the connector, and i915 only reports it
connected after a probe made with the force on; the sysfs file probes, but only when the force value
changes, hence the two writes. On amdgpu the debugfs files were enough and the extra probe is harmless. It
only accepts a connector name from a fixed pattern and only reads the EDID from
`/etc/gnome-virtual-display/edid.bin`, or generates one itself from a `WIDTHxHEIGHT@HZ` it validates.
The card is found through its connector and kernel driver, never by index: card numbers change when
a second GPU is plugged.

The EDID comes from [`host/make-edid`](../host/make-edid): CVT reduced-blanking timings, the family
real laptop panels use, with a 60 Hz rate added next to the preferred one by stretching the vertical
blank at the same pixel clock. Some display engines (AMD DCN 3.x) only accept such a mode when it is
activated by a hotplug, not through the kernel's `video=` boot parameter, which is why the EDID is
injected at runtime and nothing is set at boot.

## Layout through Mutter, never through files

GNOME's layout is changed with `org.gnome.Mutter.DisplayConfig.ApplyMonitorsConfig`, always with
method 1 (temporary). Method 2 (persistent) opens the "Keep these display settings?" prompt and Mutter
reverts everything after 20 s when nobody answers, which, during a stream, nobody does.

Rules the tool keeps:

- **Your monitors are yours.** Outside streams the tool learns what you set in GNOME Settings
  (position, rotation, scale, mode, primary, monitors switched off) into
  `~/.local/state/gnome-virtual-display/prefs.json` and never changes it. During a stream it learns
  nothing about them. At the end of a stream (`base`) and at login (`boot`) it puts that layout back,
  which also repairs Mutter falling back to its defaults after the virtual monitor was plugged.
- **Every layout is fitted to Mutter's rules** before it is applied: no overlap, every monitor
  touching another, origin at (0, 0). A monitor that overlaps or floats (unplugged neighbour, new
  scale or rotation) slides against the right edge of the monitors on its left, in the same
  left-to-right order; the whole layout is then shifted to start at (0, 0).
- **Extend** places the virtual monitor against the right edge of the rightmost monitor, vertically
  centred. Whatever the number of monitors, it cannot overlap one, and the pointer crosses on the whole
  edge. A position you chose yourself in Settings is kept as long as it touches a monitor and overlaps
  none.
- **The virtual monitor is primary during a stream** (GNOME shows the top bar and the dock on the
  primary monitor only). The primary role of your monitors is remembered from outside streams and
  given back afterwards, to the same monitor.
- **No mirror, ever.** A layout where the virtual monitor shares a logical monitor with a physical one
  is refused and never remembered.
- **Scales are read from Mutter** (`supported-scales` of each mode), never computed: Mutter only
  accepts scales that divide the resolution into whole logical pixels (1.25 works on 2400x1600, 1.5 does
  not). When the remembered scale does not fit a new client resolution, Mutter's preferred scale for
  that mode is used.
- `~/.config/monitors.xml` only ever describes the physical monitors. Mutter matches a stored
  configuration against every monitor it names, disabled ones included, and the virtual monitor does
  not exist at boot: a file that names it never matches, and Mutter starts with its defaults.

## The life of a stream

1. At login the user service runs `boot`: the connector is plugged (switched off in the layout) and
   the remembered layout applied. Plugging at rest avoids a hotplug in the middle of a stream start.
2. Moonlight opens an entry. Sunshine runs `prep MODE`: it marks the session, writes a new EDID when
   `match_client` is on and the current one lacks the client's mode (from `SUNSHINE_CLIENT_WIDTH`,
   `_HEIGHT`, `_FPS`), and applies the mode. Sunshine then captures the connector through KMS.
3. `wait MODE` runs as the app's detached command and `hold` as its command: while `hold` runs,
   Sunshine keeps the app launched. `wait` counts the open sessions in Sunshine's log (or journal):
   a client that reconnects before its old session timed out has two for a moment, and the old
   one's timeout must not end the new one.
4. **Clean end** (the client quits the app): Sunshine stops `hold` and runs `undo`: base at once.
   **Dropped stream** (Wi-Fi lost, client killed, stream window closed without quitting): 6 s after
   Sunshine logs the end (a Quit reaches Sunshine up to about 5 s after the end of its stream, and
   must still restore base), `wait` releases the app. `hold` exits once no session is open (Sunshine
   ends the open sessions when the app's command exits), Sunshine frees the app and runs `undo`,
   which keeps the virtual layout for `grace` seconds. A reconnection in that time is a new launch:
   `prep` runs with that client's resolution on the layout still in place. Sunshine runs no command
   on a resume, so a resumed app would keep the previous client's resolution (a laptop that moved
   its Moonlight window from a 16:9 monitor to its 3:2 panel would get black bars). Without a
   reconnection, `wait` restores base. Entries written by an older install (no `hold`) keep the
   old behaviour, base and a Sunshine restart after the grace period, until `./install.sh` or
   `gnome-virtual-display sync-apps` updates them.
5. A newer stream wins: `prep` and `wait` share a session token, and an old `wait` whose token was
   replaced leaves without touching the layout.

`base` switches the virtual monitor off but leaves the connector plugged: an unplug and replug cycle
under a running gnome-shell has crashed it. On a host with no physical monitor the virtual one is
the only screen: base keeps it on.

## Moonlight entries

`sync-apps` (also run by `reapply` before Sunshine starts, and by the installer) keeps our entries
in Sunshine's `apps.json` current: the format above, matched on their `prep` command so that
renamed entries are updated too, and **Extend** only when a physical monitor is plugged in (read
from sysfs, every GPU, the virtual connector left out). A hidden Extend is set aside as it is, with
its place in the list, in `~/.local/state/gnome-virtual-display/hidden-apps.json`. Sunshine reads
`apps.json` when it starts: after a monitor is plugged or unplugged, the watcher restarts it between
streams.

## The watcher

`gnome-virtual-display watch` (user service) repairs what can go wrong between streams, and stays out
of the way while an app is engaged (between `prep` and the client's connection the log shows no
session yet; acting then would switch the virtual monitor off right before the capture starts):

- outside streams: learns the layout you set in GNOME Settings, and switches off a virtual monitor
  left on (the physical monitors then get their remembered layout back);
- Sunshine stuck on an app after a dropped stream, or a session that captured a physical monitor:
  Sunshine is restarted between sessions, at most once every 120 s;
- Xwayland frozen (see below): GDM is restarted.

Each call to Mutter is bounded to 5 s. A frozen gnome-shell gives exit code 3 and a line in
`journalctl -t gnome-virtual-display`, instead of a 25 s D-Bus timeout per call and a "failed to start"
in Moonlight.

## Login screen

The login screen picks the largest monitor as primary, and the virtual one is often the largest:
the login prompt would sit on an invisible screen. A system unit unplugs the virtual monitor before
every GDM start, a user unit unplugs it at the end of each session, and the next session plugs it back.
After a reboot, Sunshine (started by the user manager before any session) streams the login screen,
so you can log in from the client.

## Xwayland freeze

With some X11 clients that present frames through the Present extension (browsers forced to X11 with
a discrete GPU, for example), switching a physical monitor off can send Xwayland into an endless loop
in `xwl_present_flush_blocked`. gnome-shell waits for it and stops answering; the desktop is lost and
killing Xwayland does not bring it back. The tool recognises the signature (Xwayland at 100 % CPU with
that function on its stack), reports it, and after seeing it for 60 s the watcher restarts GDM: the
login screen can be streamed, the frozen desktop could not. Running browsers as native Wayland clients
avoids the trigger.
