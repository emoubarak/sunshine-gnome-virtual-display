# Troubleshooting

Start with `gnome-virtual-display list` (outputs, modes, layout) and the logs:
`journalctl -t gnome-virtual-display`, `journalctl --user -u gnome-virtual-display-watch`,
`~/.config/sunshine/sunshine.log`.

## Moonlight says "failed to start"

- `Couldn't map '<connector>' to a monitor index` in Sunshine's log: the virtual monitor was not
  plugged when the capture started. Check `sudo -n /usr/local/sbin/gnome-virtual-display-drm on <connector>`
  works without a password prompt (the sudo rule names the connector; run `./install.sh` again after
  changing it).
- Exit code 3 in Sunshine's log (`exited with code [3]`): gnome-shell did not answer within 5 s. The
  cause is in `journalctl -t gnome-virtual-display`; see "Frozen desktop" below.

## The stream shows a physical monitor instead of the virtual one

Sunshine skipped the `prep` command because it still believed the previous app was running (the
client vanished without quitting). The watcher restarts Sunshine between sessions when it sees this;
to do it at once: `systemctl --user restart <sunshine unit>`. Enable "Quit app on host PC after ending
stream" in Moonlight to make clean ends the norm.

## The pointer cannot leave the streamed screen

Moonlight is in absolute mouse mode: turn off "Optimize mouse for remote desktop instead of games".

## My monitor came back rotated or at the wrong scale

After a stream the tool puts back the layout it learnt before it, from
`~/.local/state/gnome-virtual-display/prefs.json`. Set your monitors in GNOME Settings outside a
stream and it learns them within seconds. To start over: delete `prefs.json`, then run
`gnome-virtual-display base`.

## Black screen on the host

```bash
gnome-virtual-display base                                     # physical monitors back, virtual off
sudo /usr/local/sbin/gnome-virtual-display-drm off <connector>   # remove the virtual monitor (avoid during a session)
sudo systemctl restart gdm                                     # last resort: back to the login screen
```

## Frozen desktop (mouse and keyboard dead, Moonlight times out)

Check whether Xwayland is looping: `eu-stack -1 -p $(pgrep -x Xwayland)` shows
`xwl_present_flush_blocked` (needs elfutils; prefix it with `sudo` where `kernel.yama.ptrace_scope`
is 1, the default on Ubuntu and Arch: the automatic detection then cannot read the stack either and
only reports a slow gnome-shell). The desktop cannot be recovered; `gnome-virtual-display fix-session`
restarts GDM (the watcher does it by itself after 60 s). Run browsers and Electron apps as native
Wayland clients to avoid it.

## Sunshine crashes when it stops

Some Sunshine versions call the app's terminate handler twice (`proc_t::terminate`) and abort with
`ABRT`, sometimes on a plain `systemctl stop`. systemd restarts it. Avoid restarting Sunshine right
after a stream when it is not needed.

## The virtual monitor does not take the client's resolution

`match_client = true` in the config, and the client must ask for a mode the EDID can carry: an EDID
1.4 detailed timing stops at a 655.35 MHz pixel clock (3840x2160 tops out near 74 Hz). Above that,
the tool keeps the client's resolution at 60 Hz. The EDID written last is decoded by
`make-edid --decode /etc/gnome-virtual-display/edid.bin`.
