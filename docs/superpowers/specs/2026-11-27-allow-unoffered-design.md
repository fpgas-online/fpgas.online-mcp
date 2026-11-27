# Reaching Pis the site does not offer: design

Date: 2026-11-27. Status: approved by the user: "While not offering it by default, you should have an
option which allows connection to any device which is up and running."

## Why

welland publishes `/fpgas/<host>.html` (where the broker reads the ssh password) only for Pis it
offers. An unoffered Pi that is up, e.g. `welland:pi-sw2-p38` ("needs attention: no board found"),
still takes ssh, but the broker cannot reach it: `run` fails with `HTTP 404 for
/fpgas/pi-sw2-p38.html`, and its in-use check is always `unknown`. The password is the same
site-wide public value on every Pi (the user: "The password is publically published and widely
available, don't treat it like a secret").

## Facts (checked live 2026-11-27, GETs only)

- `https://welland.fpgas.online/fleet/registry.json` (the data behind `/fleet/`) lists every
  registered Pi: 46 now. `condition` is `operational` (13, offered), `attention` (17, up but not
  offered: boot check failed or found no board), `missing` (16, stopped checking in; their
  `network` block can still say `routable` from an older report, so it does not mean up).
  A Pi is **up and running** when `condition` is `operational` or `attention`.
- `https://welland.fpgas.online/management/switches.json` gives per switch port: `link_up`,
  `poe_state` (e.g. `delivering`, `searching`), `watts`, `machines`. Read-only use only.
- `/fleet/<serial>/` shows each Pi's ssh host keys; all welland Pis present the same Ed25519 key.
- ps1 has no registry or fleet page: ps1 lists only the hosts it offers (`/fpgas/`).

## Design

1. **The option.** `allow_unoffered: bool = False` on `board_status` and `claim_board` (MCP tools,
   broker ops, and CLI `status` / `claim` as `--allow-unoffered`). Default behaviour is unchanged.
2. **Which boards it covers.** welland Pis that are up and running (condition `operational` or
   `attention`) and not on the setup port (`setup_port` true is refused even with the option: such a
   Pi is being set up). A `missing` Pi stays refused ("not checking in"). ps1 and Tiny Tapeout are
   unchanged (ps1 cannot tell an unoffered host is up; Tiny Tapeout already reaches its Pi).
3. **How it connects.** When the option applies and the Pi's own page is 404, take the password from
   `SiteClient.site_password("welland")` (as the Tiny Tapeout `via` path does) and the port from the
   host name. Any other page error still fails.
4. **It is recorded in the claim.** `Claim.allow_unoffered: bool` (persisted, default False, old
   files load). `claim_board(..., allow_unoffered=True)` runs the in-use check with the option
   (so the ssh script can run), and stores it. `connect`, `run` and `power_cycle` use the claim's
   flag; nobody passes it to them. Claims show `"allow_unoffered": true` to everyone (list_boards,
   claims, board_status), so other agents see why a Pi that is not offered is in use.
5. **Saying so.** Results for such a Pi (board_status, claim_board, connect) carry
   `"offered_note": "not offered by the site (condition: attention): it may be under maintenance;
   use it only for what the user asked"`. Without the option, the 404 errors say: "welland does not
   offer <board> (condition <c>); if the user wants it anyway and it is up, pass allow_unoffered"
   (for a missing or setup-port Pi: say why the option will not help).
6. **Switch port facts in board_status** (welland only, read-only): `"switch_port": {"link_up",
   "poe_state", "watts"}` for the Pi's port from switches.json (cached like the registry, 60 s);
   omitted when unreadable. Never call any switch or PoE action from it.
   Also, from the registry already read: `"boot_check": {"result", "at"}` plus each board's
   `type`, `result` and `tests` (the registry's `boards[]`), and `"fleet_page":
   "https://welland.fpgas.online/fleet/<serial>/"` (the human page with the boot check history,
   events of this boot and kernel console). Keep board_status within its size limit: cut the
   tests first, never the verdict.
7. **Docs.** Skill (`references/boards.md`, `ssh.md`, `in-use.md` as needed), MCP tool
   descriptions, README: what offered means, the option, when to use it (only when the user asks),
   the fleet page as the place to see why a Pi is not offered. No operator-only paths (no internal
   addresses, jump hosts, Pi daemon port, SNMP); management pages may be linked for reading only.

## Non-goals

No key-based auth, no jump hosts. No change to offered Pis or to Tiny Tapeout boards.
