# Network: smooth streams on a home Wi-Fi

Two things make a stream between two machines of the same Wi-Fi network slow, and neither is the
bitrate.

![The stream crosses the Wi-Fi twice, host to access point and access point to client, and the host's downloads share the first hop. Through the VPN address: 1024-byte packets, no DSCP marks, 42 ms. Through the LAN address with stream-qos: 1392-byte packets marked DSCP 40 and 48, internet TCP shaped to 40 Mbit/s, 5 to 9 ms](images/network.svg)

## Measurements

One host (Wi-Fi 6, 160 MHz, 5 GHz), one laptop client on the same access point, Moonlight 6.2,
2400x1600 at 120 fps and 150 Mbit/s, a moving test pattern, 25 s per run. "Network latency" is the
figure from Moonlight's end-of-stream statistics.

| Setup | Network latency (variance) | Frames lost |
|---|---|---|
| Host reached through its VPN address (Tailscale), downloads running on the host (~170 Mbit/s) | 42 ms (23) | a few, stream frozen waiting for a key frame |
| LAN address, same downloads | 14 to 23 ms (10 to 17) | 0 to 0.8 % |
| LAN address, same downloads, stream at 80 Mbit/s | 26 ms (12) | 0 |
| **LAN address + `stream-qos`** | **5 to 9 ms (0 to 4)** | **0** |

## 1. Use the LAN address, not the VPN

When Moonlight reaches the host through a VPN interface (Tailscale, WireGuard), it treats the stream
as remote (`RI_VPN` in Moonlight's `nvcomputer.cpp`):

- video packets are capped at 1024 bytes instead of 1392, so about 35 % more packets per frame, each
  one also encrypted and wrapped again;
- the client asks for no QoS (`qosTrafficType=0` in moonlight-common-c), so Sunshine does not tag
  the video with DSCP 40 and the audio with DSCP 48. Those tags are what Wi-Fi multimedia (WMM) uses
  to send the video before bulk traffic. Even when they are set, a VPN tunnel does not copy them to
  the outer packet.

Moonlight tries the addresses it knows in order (local, remote, manual) and stores as "local" the
address through which it reached the host last time. Add the host once through its VPN address (the
fallback when away), then on a Linux client start Moonlight with
[`client/moonlight-lan`](../client/moonlight-lan): before each start it puts the LAN address back,
but only if the machine answering there is the paired host (same `uniqueid`).

```bash
mkdir -p ~/.config/gnome-virtual-display
printf 'HOST=myhost\nLAN_ADDRESS=192.0.2.10\n' > ~/.config/gnome-virtual-display/moonlight-lan.conf
ln -s "$PWD/client/moonlight-lan" ~/.local/bin/moonlight-lan
```

Then use `moonlight-lan` as the `Exec=` of your Moonlight desktop entry (copy
`com.moonlight_stream.Moonlight.desktop` to `~/.local/share/applications/` and change that line).
Reserve the host's address in your router's DHCP settings so it does not change.

A host that is sometimes on Ethernet has one address per network card. Reserve each one (Wi-Fi and
every Ethernet port: the router reserves by MAC address) and list them, Ethernet first:
`LAN_ADDRESS="192.0.2.20 192.0.2.10"`. They are probed at once and the first one that answers as
the paired host wins, so the client follows the cable without any change. With both the cable and the
Wi-Fi connected, the host already sends through the cable (NetworkManager gives Ethernet the lower
route metric); the Ethernet address makes the client's packets take it too.

Several hosts paired with the same client: repeat the pair in the same file, each `LAN_ADDRESS`
line after its `HOST` line. Each host is checked on its own before Moonlight starts.

```
HOST=desktop
LAN_ADDRESS="192.0.2.20 192.0.2.10"
HOST=laptop
LAN_ADDRESS="192.0.2.30"
```

Sunshine's own `packetsize` setting only caps the size the client asks for: leave it at 0.

## 2. Shape the host's downloads during a stream

On one Wi-Fi channel, the stream goes through the air twice: host to access point, access point to
client. A large download on the host (models, updates, backups) takes most of the airtime, and the
latency of the stream follows. Lowering the stream bitrate does not change that, the download is the
problem.

`./install.sh --stream-qos` installs:

- `stream-qos` (root): while on, incoming TCP from outside the LAN goes through an `ifb` interface
  shaped by CAKE at 40 Mbit/s in total, shared per connection. Downloads keep going, slower, and
  a web page or an API call next to them stays as fast as without shaping. The LAN, link-local and
  the IPv6 prefix of the Wi-Fi, and all UDP (VPN tunnels, QUIC, a remote client) are never shaped.
  Nothing happens when the host is on Ethernet.
- `stream-qos-watch` (user service): follows Sunshine's journal and keeps the shaping on while at
  least one session is active. It recognises every session end Sunshine logs, including a client
  that vanished (`Ping Timeout`, no `CLIENT DISCONNECTED` line) and a Quit from Moonlight (only
  `Executing Undo Cmd`), and checks the real state every 60 s.

Change the rate in `/etc/gnome-virtual-display/stream-qos.conf` (`RATE=40mbit`). A first version
used one shared policer: it dropped 15 % of all packets, and ordinary web pages timed out next to the
downloads. Per-connection fairness is what keeps the rest of the machine usable.

Check during a stream: `/usr/local/sbin/stream-qos status` (no root needed), `journalctl -t stream-qos`.

## Still the best fix

An Ethernet cable from the host to the router: one Wi-Fi hop instead of two, and no airtime shared
with the host's downloads. Nothing else to change: download shaping turns itself off when the
default route is not Wi-Fi, and `moonlight-lan` picks the Ethernet address when it is listed first
(see above).
