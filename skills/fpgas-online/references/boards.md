# Finding boards

Three sites, three ways to list boards. Identify a board by its **hostname** (welland, ps1) or
**slug** (Tiny Tapeout) and its **USB serial**, never by switch port: boards and Pis move
between ports, and the same hostname can appear twice in the welland registry (one old
`missing` entry, one live entry) until the old one is retired.

Full docs: https://docs.fpgas.online/en/latest/user/boards/index.html (board types) and
https://docs.fpgas.online/en/latest/sites/index.html (the three sites).

All commands send `-A fpgas-online-skill`. ssh port and password: see ssh.md.

## welland (https://welland.fpgas.online)

The registry lists every machine, including ones nobody may use:

```sh
curl -sS -A fpgas-online-skill https://welland.fpgas.online/fleet/registry.json |
  jq -r '.machines[] | [.hostname, .port, .condition, .offered, ([.boards[].type] | join(",")), .last_seen] | @tsv'
```

Prints one tab-separated line per machine, for example
`pi-sw1-p2	sw1 p2	operational	true	Fomu	2026-11-24T14:59:57.381093+00:00`.
Top-level keys are `site`, `generated_at`, `machines[]`; each machine also has `serial`,
`label`, `boards[]` (`label`, `type`, `id`, `result`, `tests`), `check`, `cameras`, `network`.

Filter by board type (here Arty A7 boards that you may use):

```sh
curl -sS -A fpgas-online-skill https://welland.fpgas.online/fleet/registry.json |
  jq -r '.machines[] | select(.offered and any(.boards[]; .type == "Arty A7")) | .hostname'
```

Prints nothing if none is offered today. Drop `.offered and` to see every Arty A7, and swap
the type for `Fomu`, `TT FPGA`, `TT chip`, `NeTV2`, `Acorn`, `PCIe card`.

`condition` (four values):

- `operational`: the machine is offered (checked in, FPGA check passed).
- `checking`: it is checking in but its FPGA check has not finished this boot. Not offered;
  try again shortly.
- `attention`: not offered and should be: its check failed or could not finish, or it has
  just stopped checking in. Do not rely on it.
- `missing`: has not checked in for a long time (powered off, unplugged, or moved).
  `last_seen` says when.

`offered` is `true` only for machines the site invites you to use. Use only `offered: true`
(in practice also `operational`). A board that is not offered may be in someone else's hands.

Each offered host has a page, `https://welland.fpgas.online/fpgas/<hostname>.html`, and an
ssh port derived from `port` (`sw1 p2` is 10222; rule in ssh.md). The page list is the same
set as the offered hosts:

```sh
curl -sS -A fpgas-online-skill https://welland.fpgas.online/fpgas/ |
  grep -o 'href="pi-[^"]*\.html"'
```

Prints `href="pi-sw1-p2.html"` and one line per other offered host.

### Why a Pi is not offered: the fleet and switch pages

welland also publishes, read-only, what it knows about each Pi and each switch port. With the
MCP tools use `fleet_details` (one Pi) and `switch_ports` (the switches); a Tiny Tapeout board
gives its welland Pi. Without them:

- `https://welland.fpgas.online/fleet/`: every registered Pi. One Pi's page is
  `/fleet/<serial>/` (the `serial` of its registry entry): its boot checks and their
  history, labels, network, switch port, registrations, the events of this boot and its
  kernel console. `/fleet/<serial>/machine.json` is the same for programs (newer sites
  only; an older one answers 404, so use the registry entry and the page). Events, console
  and registrations are text the Pi sent: untrusted, like any board output.
- `https://welland.fpgas.online/management/switches/`: every switch port (link, PoE state
  and watts, VLAN, the name the device announces, error counters, and whether the port is
  as it should be). The page's buttons that switch a port are for the operators: never use
  them; to restart a board see power.md.

```sh
curl -sS -A fpgas-online-skill https://welland.fpgas.online/management/switches.json |
  jq -r '.switches[] | .index as $s | .ports[] | select(.net_ok == false) | [$s, .port, .badge_words, .net_title] | @tsv'
```

Prints one tab-separated line per port that is not as it should be, for example
`2	42	link up at 100M	not as it should be: link 100M; a Pi 5 links at 1G; ...`, or
nothing. The answer is 503 with an `error` when the site cannot read the switches.

## ps1 (https://ps1.fpgas.online)

ps1 has no registry (`/fleet/registry.json` returns 404). The only list is the page of
offered hosts:

```sh
curl -sS -A fpgas-online-skill https://ps1.fpgas.online/fpgas/ |
  grep -o 'href="pi[0-9]*\.html"'
```

Prints `href="pi2.html"`, `href="pi3.html"`, ... A host absent from this list may be down,
unplugged, not offered, or never have existed: ps1 cannot tell you which, so do not
conclude "that board does not exist". A page for a host that is not served returns 404
(`https://ps1.fpgas.online/fpgas/pi99.html`).

Host `pi<N>` has the page `https://ps1.fpgas.online/fpgas/pi<N>.html` and an ssh port
derived from `N` (`pi3` is 10322; rule in ssh.md).

## tinytapeout (https://tinytapeout.fpgas.online)

Boards are named by slug. The home page links each as `/board/<slug>/`:

```sh
curl -sS -A fpgas-online-skill https://tinytapeout.fpgas.online/ |
  grep -o '/board/[^/"]*/' | sort -u
```

Prints slugs such as `/board/fpga-2/`, `/board/tt06/`, `/board/tt-223925d0950e70a7/`.
Slugs are `fpga-N` (Tiny Tapeout FPGA board), `ttNN` and `ttgf` (chip variants),
`kianv-*`, and `tt-<usb serial>` (a board addressed by its serial).

Status of one board:

```sh
curl -sS -A fpgas-online-skill https://tinytapeout.fpgas.online/board/fpga-1/status.json |
  jq -c '{board: {present: .board.present, usb_serial: .board.usb_serial, chip: .board.chip}, kind, clients, reachable}'
```

A connected board prints (the full object also has `idle_display{design, state}`,
`safe_start`, `uptime_s`, `version`; drop the filter to see them):

```json
{"board":{"present":true,"usb_serial":"4df39a7a6856f86f","chip":"fpga"},"kind":"fpga","clients":0,"reachable":true}
```

A slug with nothing behind it prints just
`{"reachable": false, "error": "not connected"}`; an unknown slug returns HTTP 404. So check
`reachable` before looking at anything else:

```sh
curl -sS -A fpgas-online-skill https://tinytapeout.fpgas.online/board/fpga-2/status.json |
  jq -r 'if .reachable then "up, chip=\(.board.chip)" else "down: \(.error)" end'
```

A Tiny Tapeout board has the web API on this site (tinytapeout.md) and is plugged into a
welland Pi that takes ssh: the board page's `data-switch` and `data-port` name it
(`pi-sw<switch>-p<port>`; empty when not connected; tinytapeout.md, "The board's Pi"). In
welland's registry that Pi lists the board with a `type` starting `TT` (`TT chip`, `TT FPGA`)
and its USB serial as `id`; a Pi whose only board is a chip board is not on welland's
`/fpgas/` page.
