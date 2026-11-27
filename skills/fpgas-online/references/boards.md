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
`label`, `boards[]` (`label`, `type`, `id`, `result`, `tests`), `check` (the boot check's
`result` and `at`), `setup_port`, `cameras`, `network`.

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

### Pis welland does not offer

welland offers a Pi when its boot check passed. An `attention` Pi is up and takes ssh, but
its check failed or found no board (it may be under maintenance): use one **only when the
user asks**, and only for what they asked. With the fpgas-online MCP tools pass
`allow_unoffered` to `board_status` and `claim_board` (the claim keeps it for `connect`,
`run` and `power_cycle`, and shows it to others); without them, ssh in as usual (it has no
`/fpgas/` page; same password: ssh.md, "A Pi welland does not offer"). Check it is not in use
first (in-use.md). Not for a `missing` Pi (stopped checking in; a `routable` `network` block
may be from an older report) or one with `setup_port: true` (being set up).

Why a Pi is not offered: its fleet page `https://welland.fpgas.online/fleet/<serial>/` (the
boot check history, the events of this boot and the kernel console; `board_status` gives the
link, with `boot_check`, `boards` and `switch_port`). Switch ports (link, PoE power):
https://welland.fpgas.online/management/ (read it only; never use its buttons).

Each offered host has a page, `https://welland.fpgas.online/fpgas/<hostname>.html`, and an
ssh port derived from `port` (`sw1 p2` is 10222; rule in ssh.md). The page list is the same
set as the offered hosts:

```sh
curl -sS -A fpgas-online-skill https://welland.fpgas.online/fpgas/ |
  grep -o 'href="pi-[^"]*\.html"'
```

Prints `href="pi-sw1-p2.html"` and one line per other offered host.

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
