# Power-cycling a board

A power-cycle switches the board's PoE port off, waits half a second, and switches it on.
The host reboots from the network, so **everything on it is lost**: every ssh session,
your files in `~/Uploads`, the loaded design. It also ends anybody else's session on that
board. Do it only when the board is hung or you need a clean start, never on a board that
someone is using (in-use.md), and tell the user before you do it.

Full docs: https://docs.fpgas.online/en/latest/user/boards/reset.html

All commands send `-A fpgas-online-skill`. ssh setup (askpass, `fo-*` aliases): ssh.md.

## Before you cycle

1. Run the in-use check for the board (in-use.md). Skip a board that is in use.
2. Close your own ssh master, so it does not sit on a dead socket afterwards:
   ```sh
   ssh -O exit fo-ps1-pi21   # your alias; prints "Exit request sent."
   ```
   (If it prints `Control socket connect(...): No such file or directory` there was no
   master; that is fine.)
3. Note the time: `T0=$(date +%s)`.

## The request

One POST, no login. It must carry `Content-Type: application/json`. Do not add `Origin` or
`Sec-Fetch-Site` (the site refuses a cross-site request with 403 `refused a ... request`;
a plain `curl` sends neither, which is accepted). A request without the JSON
content type gets 403 `expected Content-Type: application/json; nothing was sent to the switch`.

The body names the switch port. Take it from the host name (boards.md): `port` is a
**string** (ps1 answers an integer with HTTP 500; welland accepts either).

| Site | Body | Where the numbers come from |
| --- | --- | --- |
| welland | `{"port":"2","switch":1}` | host `pi-sw<S>-p<P>`: `switch` = S (a number), `port` = P |
| ps1 | `{"port":"21"}` | host `pi<N>`: `port` = N; no `switch` (ps1 has one switch) |
| tinytapeout | `{"port":"9","switch":2}` | the `data-port` and `data-switch` attributes of the board page (see below) |

Read-only state check (never changes anything; works on all three sites):

```sh
curl -sS -A fpgas-online-skill -X POST -H 'Content-Type: application/json' \
  -d '{"port":"2","switch":1}' https://welland.fpgas.online/snmp/status
```

Prints `{"state": "on"}` (ps1 pretty-prints the same JSON over several lines).

The cycle:

```sh
curl -sS -A fpgas-online-skill -X POST -H 'Content-Type: application/json' \
  -d '{"port":"2","switch":1}' https://welland.fpgas.online/snmp/toggle
```

Returns after about 1-2 s with `{"2": ["off", "on"]}` (the switch's confirmed state after
each step; the key is the port). Replace the host, and for ps1 use
`-d '{"port":"21"}' https://ps1.fpgas.online/snmp/toggle`.

Other answers (JSON `{"error": ...}`): 403 not a board this site offers (nothing was
sent); 400 bad body; 502 the switch did not confirm (`The port may be off: press Reset
again, it can be tried again at once`); 503 the site is not configured.

### Tiny Tapeout: slug to port (from the site's code; not cycled live)

A Tiny Tapeout slug has no port in its name. Read it from the board page, which is what
the "Power-cycle board" button sends:

```sh
curl -sS -A fpgas-online-skill https://tinytapeout.fpgas.online/board/fpga-2/ |
  grep -o 'data-\(port\|switch\)="[^"]*"'
```

Prints `data-port="..."` and `data-switch="..."`; both are empty (`""`) when no board is
connected to that slug, in which case there is nothing to cycle. Then POST that port and
switch to `https://tinytapeout.fpgas.online/snmp/toggle`, as above. The button is hidden
and the page has no controls for a slug without a port. Only cycle a board whose
`status.json` shows `reachable: true` and `clients: 0` on two reads 15 s apart
(tinytapeout.md).

## The hold: reading 429 and `Retry-After`

welland holds a port after a switch (Tiny Tapeout runs the same code; not measured there): 60 s after a cycle or switch-on,
10 s after a switch-off. A second request inside the hold is refused and nothing is sent to
the switch. Measured on welland:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 59
{"error": "switch 1 port 2 was switched a moment ago; try again in 59 seconds. Nothing was sent to the switch"}
```

Read the header, sleep that long, and try at most once more. Do not loop. `curl -i` shows
the status line and headers:

```sh
curl -sS -i -A fpgas-online-skill -X POST -H 'Content-Type: application/json' \
  -d '{"port":"2","switch":1}' https://welland.fpgas.online/snmp/toggle
```

A `429` response has a `Retry-After: <seconds>` header; the body says why.

**ps1 differs: no hold was seen.** Two cycles of `pi21` two seconds apart both returned
`["off", "on"]` and no 429. Do not rely on the site to stop a double cycle there; wait
60 s yourself.

## Waiting for the board to come back

Poll once every 10 s and give up after 5 minutes (30 tries). Do not cycle again if it does
not return. `T0` is the epoch time you noted before the request.

welland: the registry's `last_seen` must be newer than T0 (the Pi has checked in from its
new boot), then ssh must answer:

```sh
H=pi-sw1-p2; A=fo-welland-pi-sw1-p2
for i in $(seq 1 30); do
  ls=$(curl -sS -A fpgas-online-skill https://welland.fpgas.online/fleet/registry.json |
    jq -r --arg h "$H" '[.machines[] | select(.hostname == $h) | .last_seen] | max')
  if [ "$(date -u -d "$ls" +%s)" -gt "$T0" ] && ssh -o ConnectTimeout=5 "$A" 'uname -n'; then
    echo "back after $(( $(date +%s) - T0 )) s"; break
  fi
  sleep 10
done
```

ps1 (no registry): ssh answering is the test. A host that is still booting gives
`Connection refused` or a timeout; keep polling:

```sh
A=fo-ps1-pi21
for i in $(seq 1 30); do
  if ssh -o ConnectTimeout=5 "$A" 'uname -n'; then
    echo "back after $(( $(date +%s) - T0 )) s"; break
  fi
  sleep 10
done
```

Prove the host really restarted, not just that ssh answers: ssh may still reach the old
boot for the second or two before power drops. Compare `ssh "$A" 'uptime -s'` before and
after; the boot time must move. On the one ps1 cycle measured (`pi21`) it did **not** move:
the answer said `["off","on"]` but the host's uptime stayed at 7 days, so the port may not
feed that host. If the boot time did not move, report it and do not cycle again.

tinytapeout: `status.json` must show the board again:

```sh
S=fpga-1
for i in $(seq 1 30); do
  if curl -sS -A fpgas-online-skill "https://tinytapeout.fpgas.online/board/$S/status.json" |
    jq -e '.reachable == true and .board.present == true' > /dev/null; then
    echo "back after $(( $(date +%s) - T0 )) s"; break
  fi
  sleep 10
done
```

`jq -e` exits non-zero for `false` and for the `{"reachable": false, "error": ...}` object
of a board that is still down.

### Measured return times (2026-11-24)

| Site | Host | Request returned | Back |
| --- | --- | --- | --- |
| welland | `pi-sw1-p2` (Fomu) | 1.0 s | registry `last_seen` newer at about 62 s; ssh answered at 67 s |
| ps1 | `pi21` | 1.6 s | ssh answered at 15.6 s, but no reboot happened (see above) |
| tinytapeout | not measured | | no idle reachable board existed |

These are one measurement each at 10 s polling; plan for a welland board to take a minute
or two. Give up at 5 minutes.

## If it does not come back

Stop. Do not send another toggle (it is held for 60 s and a hung board does not need a
cascade of cycles). Tell the user: which host, the time of the request, the response body,
the last `last_seen` or `status.json`, and what ssh printed. Check
`/snmp/status` once (read-only): `{"state": "on"}` means the port has power and the host
itself is not booting; `off` means the port is off and needs a person (try one `toggle`
after the hold, once).

Afterwards, close your master again (`ssh -O exit`) so the next ssh makes a fresh one.
