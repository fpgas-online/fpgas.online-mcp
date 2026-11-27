# Power-cycling a board

A power-cycle switches the board's PoE port off, waits half a second, and switches it on.
The host reboots from the network, so **everything on it is lost**: every ssh session,
your files in `~/Uploads`, the loaded design. It also ends anybody else's session on that
board. Do it only when the board is hung or you need a clean start, never on a board that
someone is using (in-use.md), and tell the user before you do it.

Full docs: https://docs.fpgas.online/en/latest/user/boards/reset.html

All commands send `-A fpgas-online-skill`. ssh setup (askpass, `fo-*` aliases): ssh.md.

## A board that is down (hung)

A board that does not answer reads `unknown` in the in-use check, and you cannot record its
boot (step 2 below). With the MCP tools: claim it with `override_reason` while it reads
`unknown` (only when the user agrees; the reason is shown to everyone) and `power_cycle`
cycles it. An override given when the board read `in use`, or a claim taken while it was
idle, does not cover it: release the claim and claim it again. It never cycles a board that
reads `in use`, override or not. With no boot recorded, the board is back only when it
answers with a boot younger than the toggle: Tiny Tapeout, `uptime_s` (the uptime of the
daemon on the board's Pi, which restarts with the Pi) smaller than the seconds since the
toggle; welland and ps1, the host's uptime (`/proc/uptime`, first field) smaller than the
seconds since the toggle, read over ssh together with its boot id. That it answers is never
enough on its own. If it does not come back, see the last section: do not
cycle it again.

By hand, the same check for welland or ps1 (`A` and `T0` as below):

```sh
out=$(ssh -o ConnectTimeout=5 "$A" 'cat /proc/sys/kernel/random/boot_id /proc/uptime') || out=
up=$(printf '%s\n' "$out" | sed -n '2s/ .*//p'); up=${up%.*}
if [ -n "$up" ] && [ "$up" -lt $(( $(date +%s) - T0 )) ]; then echo "back: up $up s"; fi
```

## Before you cycle

1. Run the in-use check for the board (in-use.md). Skip a board that is in use.
2. Record the boot you are about to replace, while it still runs. A restarted host has a new
   boot id (Tiny Tapeout: a new, small `uptime_s`); one that was not restarted keeps the
   old. Run the block for your site only, and stop if it prints `stop`.

   welland (the registry's boot id; no ssh needed), then close your ssh master (it would
   sit on a dead socket afterwards):
   ```sh
   H=pi-sw1-p2; A=fo-welland-pi-sw1-p2
   B=$(curl -sS -A fpgas-online-skill https://welland.fpgas.online/fleet/registry.json |
     jq -r --arg h "$H" '[.machines[] | select(.hostname == $h) | select(.network.current_boot == true) | .network.boot_id] | last') || B=
   case "$B" in ''|null) echo "no boot id; stop" >&2; false;; *) echo "$B";; esac
   ssh -O exit "$A"               # prints "Exit request sent."
   ```
   ps1 (no registry: ask the host; its alias is `fo-ps1-pi<N>`, ssh.md):
   ```sh
   N=                             # the host's number: pi<N>
   A=fo-ps1-pi$N
   B=$(ssh "$A" 'cat /proc/sys/kernel/random/boot_id') || B=
   case "$B" in ''|null) echo "no boot id; stop" >&2; false;; *) echo "$B";; esac
   ssh -O exit "$A"
   ```
   tinytapeout (the daemon's uptime; it restarts when the board's Pi reboots):
   ```sh
   S=                             # the board's slug
   U=$(curl -sS -A fpgas-online-skill "https://tinytapeout.fpgas.online/board/$S/status.json" |
     jq -r '.uptime_s') || U=
   case "$U" in ''|null) echo "board not reachable; stop" >&2; false;; *) echo "up $U s";; esac
   ```
   `B` is a UUID. (If `ssh -O exit` prints `Control socket connect(...): No such file or
   directory` there was no master; that is fine.)
3. Note the time: `T0=$(date +%s)`.
4. welland and tinytapeout: note the port's PoE state (with the MCP tools, `switch_ports`
   with the board; a Tiny Tapeout board gives its Pi's port). `delivering` with some watts
   is a powered Pi; `searching` means nothing draws power, so a cycle will not help (the
   Pi is unplugged or has no power): tell the user instead. Without the tools (`S` and `P`
   from the host name `pi-sw<S>-p<P>`):
   ```sh
   curl -sS -A fpgas-online-skill https://welland.fpgas.online/management/switches.json |
     jq -r --argjson s 1 --argjson p 2 '.switches[] | select(.index == $s) | .ports[] | select(.port == $p) | [.poe_state, .watts, .badge_words] | @tsv'
   ```
   Prints `delivering	4.2	link up at 1G` for a powered Pi with its link up.

## The request

One POST, no login. It must carry `Content-Type: application/json`. Do not add `Origin` or
`Sec-Fetch-Site` (the site refuses a cross-site request with 403 `refused a ... request`;
a plain `curl` sends neither, which is accepted). A request without the JSON
content type gets 403 `expected Content-Type: application/json; nothing was sent to the switch`.

The body names the switch port. Take it from the host name (boards.md): `port` is a
**string** (ps1 answers an integer with HTTP 500; welland accepts either).

**ps1: the toggle reaches the right port, but the host may not need it.** A toggle for `pi21`
answered `["off","on"]` and the switch did cycle port 21; `pi21` has no PoE HAT (it runs from
a USB-C supply), so it did not lose power. Other ps1 hosts checked have no PoE HAT either.
After a ps1 toggle, check the boot id (below). If it is unchanged, the site cannot reset that
host: tell the user it must be reset by hand. There is no hold on ps1.

| Site | Body | Where the numbers come from |
| --- | --- | --- |
| welland | `{"port":"2","switch":1}` | host `pi-sw<S>-p<P>`: `switch` = S (a number), `port` = P |
| ps1 | `{"port":"<N>"}` | host `pi<N>`: `port` = N; no `switch` (ps1 has one switch) |
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
each step; the key is the port). For ps1 use
`-d '{"port":"<N>"}' https://ps1.fpgas.online/snmp/toggle`.

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
and the page has no controls for a slug without a port. Only cycle a board that
in-use.md's Tiny Tapeout check calls idle. The same switch and port name the board's welland
Pi (`pi-sw<switch>-p<port>`, which the cycle restarts): close your ssh master to it first, as
for any welland host.

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
not return. `T0` is the epoch time you noted before the request. On welland and tinytapeout,
check the port's PoE state again (step 4 above, or `switch_ports`): it should be `delivering`
again; a port still `searching` after the cycle has a Pi that does not draw power, which
the user must look at.

A restarted host is "back" only when its boot id differs from `$B` (recorded above) **and**
ssh answers. ssh alone is not proof: it can still reach the old boot for a moment before
power drops, and a registry `last_seen` can come from the old boot too. If the id never
changes the host did not restart: the loops say so and exit 1.

welland: the registry's `network.boot_id` changes when the Pi has booted and checked in;
`current_boot` must be `true`. Then ssh must answer:

```sh
back=
for i in $(seq 1 30); do
  id=$(curl -sS -A fpgas-online-skill https://welland.fpgas.online/fleet/registry.json |
    jq -r --arg h "$H" '[.machines[] | select(.hostname == $h) | select(.network.current_boot == true) | .network.boot_id] | last') || id=
  if [ -n "$id" ] && [ "$id" != null ] && [ "$id" != "$B" ] && ssh -o ConnectTimeout=5 "$A" 'uname -n'; then
    back=yes; echo "back after $(( $(date +%s) - T0 )) s, new boot $id"; break
  fi
  sleep 10
done
[ -n "$back" ] || { echo "NOT restarted/back after 5 min: boot id still $B" >&2; false; }
```

ps1 (no registry): ask the host for its boot id; a host that is still booting gives
`Connection refused` or a timeout (the `if` treats that as "not yet"):

```sh
back=
for i in $(seq 1 30); do
  id=$(ssh -o ConnectTimeout=5 "$A" 'cat /proc/sys/kernel/random/boot_id') || id=
  if [ -n "$id" ] && [ "$id" != "$B" ]; then
    back=yes; echo "back after $(( $(date +%s) - T0 )) s, new boot $id"; break
  fi
  sleep 10
done
[ -n "$back" ] || { echo "NOT restarted/back after 5 min: boot id still ${id:-unreachable}" >&2; false; }
```

Measured on ps1 `pi21`: the toggle answered `["off","on"]` but the boot id (and
`uptime -s`) never changed, so the loop above reports "NOT restarted"; the host is
probably not powered by PoE. Tell the user to reset it by hand; do not cycle again.

tinytapeout: the board is back when `status.json` shows it reachable and present **and**
the daemon restarted after the toggle (`uptime_s` smaller than the seconds since `T0`). A
board that never lost power is still present, and the site caches `status.json` for 5 s, so
the loop waits 10 s before its first read:

```sh
back=
for i in $(seq 1 30); do
  sleep 10
  u=$(curl -sS -A fpgas-online-skill "https://tinytapeout.fpgas.online/board/$S/status.json" |
    jq -r 'if .reachable == true and .board.present == true then .uptime_s else empty end') || u=
  if [ -n "$u" ] && [ "$u" != null ] && [ "$u" -lt $(( $(date +%s) - T0 )) ]; then
    back=yes; echo "back after $(( $(date +%s) - T0 )) s, daemon up $u s"; break
  fi
done
[ -n "$back" ] || { echo "NOT restarted/back after 5 min: daemon uptime ${u:-unreachable}, was $U at T0" >&2; false; }
```

### Measured return times (2026-11-24)

| Site | Host | Request returned | Back |
| --- | --- | --- | --- |
| welland | `pi-sw1-p2` (Fomu) | 1.0 s | registry `last_seen` newer at about 62 s, ssh answered at 67 s (boot-id loop, second run: back at 76 s) |
| ps1 | `pi21` | 1.6 s | ssh answered at 15.6 s, but the boot id never changed: no reboot |
| tinytapeout | not measured | | no idle reachable board existed |

These are one measurement each at 10 s polling; plan for a welland board to take a minute
or two. Give up at 5 minutes.

## If it does not come back

Stop. Do not send another toggle (it is held for 60 s and a hung board does not need a
cascade of cycles). Tell the user: which host, the time of the request, the response body,
the last `last_seen` or `status.json`, and what ssh printed. Check `/snmp/status` once
(read-only): `{"state": "on"}` means the port has power and the host itself is not booting:
stop there. Only if it says `off` (the port was left off) may you send one more `toggle`,
after the hold, once; if that does not help, it needs a person.

Afterwards, close your master again (`ssh -O exit`) so the next ssh makes a fresh one.
