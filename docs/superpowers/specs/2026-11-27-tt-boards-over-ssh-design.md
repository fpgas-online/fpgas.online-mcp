# Tiny Tapeout boards over ssh: design

Date: 2026-11-27. Status: approved by the user ("Go do the checks yourself and fix the MCP server.
tinytapeout boards should have "more" functionality than the basic arty/acorn fpga boards").

## Why

The skill and the broker treat Tiny Tapeout boards as HTTP-only ("No ssh; everything is HTTP and one
WebSocket"). That is wrong. The user: "tinytapeout.fpgas.online is just a "skin" over the normal
fpgas.online interface"; "All tiny tapeout boards are currently at welland". Every Tiny Tapeout board
is plugged into a welland Pi, which takes ssh like every other welland Pi. Because the broker does
not know that:

- a chip board's in-use check is always `unknown` ("chip boards have no idle rule"), so every claim
  needs `override_reason`;
- `connect` / `run` refuse every Tiny Tapeout board (`no_ssh`);
- a claim on `tinytapeout:<slug>` and a claim on the welland Pi under it do not see each other.

Goal: a Tiny Tapeout board gets everything a welland board has (ssh, the ssh in-use check, power) and
keeps its Tiny Tapeout extras (status.json signals, designs, the site's own power toggle).

## Facts (checked live on 2026-11-27, GETs only)

- `https://tinytapeout.fpgas.online/board/<slug>/` carries `data-switch="<s>" data-port="<p>"` for a
  board some Pi reported (e.g. `tt-de641070db746f27` -> switch 2 port 7, `fpga-4` -> 2/35). Both are
  empty for a board no Pi reports (catalogue rows such as `tt09`, `fpga-2`, all "not connected").
  The site builds them from the Pi's own registration (fpgas.online-site `ttsite/boards.py`, `Page.switch`
  / `Page.port`). `BoardPage.tt_switch` / `tt_port` already parse them. The Pi is welland
  `pi-sw<s>-p<p>`. Boards move between Pis (fpga-1 moved p33 -> p34), so read the page live
  (`live=True`) whenever the host is needed, never from the 1-day page cache.
- welland `/fpgas/<host>.html` exists only for Pis the welland site lists: offered, and carrying a
  board that site shows. A Pi whose only board is a Tiny Tapeout chip board is not listed (site
  `pibfpgas/pis.py`: "an ASIC board is on tinytapeout.fpgas.online alone"), so its page is 404
  (verified: `pi-sw2-p7.html` 404 while `pi-sw2-p33.html` 200).
- Every welland Pi has the same login: the page's password is the site-wide `PI_PW` setting (site
  `pibfpgas/views.py` `render_pi`: "The Pis' login is public by design"). The ssh port follows from
  the host name (`boards.ssh_port`, `pi-sw2-p7` -> 20722).
- The serial WebSocket answers 503 `board not ready` when the board's Pi has not passed its boot
  check this boot (site `ttsite/views.py` `serial_ws`, `Page.has_commander`). That, not a missing
  bridge, explains the one 503 another session reported. The skill's "most chip boards had no usable
  bridge" came from one session's unsupported remark: remove it.
- A chip board's status.json: `kind: other`, `board.chip: asic`, `idle_display.state` always
  `not an fpga board`, `clients` an int (serial clients, as for FPGA boards).

Live ssh to a chip-board Pi was NOT tried (the permission system refused it). Tests use fakes.

## Design

1. **Host of a Tiny Tapeout board.** `async def tt_host(sites, slug) -> BoardId | None` (boards.py):
   the live board page's switch/port -> `BoardId("welland", f"pi-sw{s}-p{p}")`, or None when either is
   empty or not a plain positive integer. A SiteError propagates. Tiny Tapeout boards are all at
   welland: name that in one constant (`TT_HOST_SITE = "welland"`).

2. **ssh to the host.** ops `connect` and `run` on a Tiny Tapeout board resolve the host (none ->
   BrokerError `not_connected`: "the board page names no Pi: the board is not connected now") and use
   the host's ssh master (`via=<the TT BoardId>`). Results add `"host": "welland:pi-sw2-p7"`.
   `SshManager.ensure_master(board, via=None)` / `run(board, command, timeout_s, via=None)`: when the
   host's own page answers HTTP 404 and `via` is given, take the password from
   `SiteClient.site_password("welland")` (the first listed Pi's page from the welland `/fpgas/` index,
   whose own ssh port must match its own name, as now) and the port from the host name. Without
   `via` a 404 still fails as now (a welland Pi the site does not offer stays refused). `SiteError`
   gets a `status: int | None` attribute (HTTP status when there was one) so the 404 can be told apart.
   Never log or return the password.

3. **One claim per Pi.** `Claim.host: str | None` (persisted as `"host"`, absent/None in old files).
   `ClaimStore.claim(..., host=None)`: besides the same-board claim, a live claim on another board
   whose hardware key (`host or board`) equals the new claim's key is a `ClaimConflict`. A renewal
   keeps the stored host. `claim_board` on a Tiny Tapeout board stores the resolved host (None when
   not connected). So `tinytapeout:tt-de641070db746f27` and `welland:pi-sw2-p7` exclude each other.

4. **In-use check of a Tiny Tapeout board** (`inuse.check`): resolve the host (None -> `unknown`,
   "the board page names no Pi: the board is not connected"; SiteError -> `unknown` as now). Run, at
   the same time, the ssh script on the host (the holder discount applies when the claim is the live
   claim on the TT board: `_live_claim` compares against the TT id, the script runs on the host) and
   the site rule: FPGA boards the existing `_check_tt`; chip boards a clients rule (reachable,
   `clients` an int, 0 on two reads at least `TT_MIN_GAP_S` apart; `clients > 0` -> in use; anything
   unreadable -> unknown; `idle_display.state` is not used). Combined verdict: `in use` if either says
   so, else `unknown` if either says so, else `idle`; signals from both, the site's first. The
   "chip boards have no idle rule" path goes away.

5. **In-use check of a welland Pi that carries a Tiny Tapeout board.** When the registry's boards for
   the Pi include a Tiny Tapeout one (type starts with "TT"), find its slug: the Tiny Tapeout
   inventory board whose `usb_serial` equals that registry board's `id`. Found -> combine the ssh
   verdict with that board's site rule (as in 4). Not found -> the ssh verdict alone plus the signal
   "carries a Tiny Tapeout board the Tiny Tapeout site does not list". A web visitor on the Tiny
   Tapeout page then blocks a claim on the Pi too.

6. **Power.** Unchanged toggle (Tiny Tapeout boards through their own site; proof by `uptime_s`), but
   close the host's ssh master before the toggle when the host is known.

7. **board_status** of a Tiny Tapeout board adds `"host"` (live, omitted when none).

8. **Docs.** Skill `references/tinytapeout.md`, `in-use.md`, `ssh.md` (and SKILL.md's site table if
   it says Tiny Tapeout has no ssh), the MCP tool descriptions/instructions, README and the
   2026-11-25 spec: Tiny Tapeout boards are reached by ssh on their welland Pi as well as the web API;
   how to find the Pi (the page's switch/port); the in-use rule now combines both; the 503 meaning;
   drop "no usable bridge". No operator-only paths (no internal addresses, no jump hosts, no Pi
   daemon port, no SNMP).

## Non-goals

No WebSocket serial client in the broker. No change to how designs/bitstreams are loaded.
