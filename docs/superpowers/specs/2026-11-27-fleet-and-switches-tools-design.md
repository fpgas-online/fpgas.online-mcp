# Fleet and switch details in the MCP: design

Date: 2026-11-27. Status: approved by the user: "Expand the MCP so it can surface all the details
from /fleet and switches".

## Why

welland's `/fleet/` (every registered Pi), `/fleet/<serial>/` (one Pi: boot checks and their
history, labels, network, switch port, registrations, events of this boot, kernel console) and
`/management/switches/` (every switch port: link, PoE, VLAN, LLDP, errors) explain why a board is
not offered, why it is down and what it is doing. Agents see none of it: they get a 404 and guess.

## Sources (read-only, GETs only)

- `/fleet/registry.json`: the list, a documented contract (already read by the broker).
- `/fleet/<serial>/machine.json`: one Pi, everything its page shows. **New**: added to
  fpgas.online-site in parallel (branch `machine-json`); the contract is below. Until the site is
  deployed it answers 404: the broker then falls back to the registry entry, the switch port row and
  the page link, and says that the full details need the newer site.
- `/management/switches.json`: every switch and port (`{"switches": [...]}`; 503 with `error` when
  the switches cannot be read).

ps1 has none of these: the tools say so for ps1 boards. A Tiny Tapeout board resolves to its welland
Pi (`boards.tt_host`) and gets that Pi's details.

## Tools (MCP, broker ops, and CLI)

1. **`fleet_details(board, sections=None, limit=None, offset=0)`** (read-only, idempotent,
   open-world). Default sections: `machine`, `boards`, `network`, `switch_port`. Others on request:
   `boot_checks`, `labels`, `registrations`, `events`, `console`; `"all"` for every one. List
   sections are paged (`limit`, `offset`, newest first; the result says how many there are and the
   next offset). The result fits the tool output limit (8192 bytes): cut the longest section first
   and say what was cut and how to get it. `events`, `console` and `registrations` are text the Pi
   sent: wrap them as untrusted board output, like `run` output. Includes `"page"` (the human URL).
   CLI: `fpgas-online fleet <board> [--section ...] [--limit N] [--offset N] [--json]`.
2. **`switch_ports(site="welland", switch=None, port=None, board=None, problems=False)`**
   (read-only). Each switch: name, model, status, read_at, known_issues, empty_ports. Each port a
   compact row: switch, port, machines (host names), link_up, speed, poe_state, watts, vlan,
   lldp_name, lldp_port, rx_errors, tx_errors, net_ok, net_title, badge_words, empty. Never the
   page's action fields (`*_action`, `power_*`, `link_action`, `can_power`): the tool cannot act.
   `board` selects that board's port (a TT board through its Pi). `problems` keeps ports whose
   `net_ok` is false or that have errors. Paged and size-limited like 1. CLI: `fpgas-online
   switches [--switch N] [--port N] [--board B] [--problems] [--json]`.
3. **`list_boards`** gains nothing new, but its welland note points at `fleet_details` for why a
   board is not offered.
4. **Guide and docs.** Skill references (boards.md: the fleet pages and the two tools; power.md:
   check `switch_ports` PoE state before and after a cycle), MCP instructions/tool descriptions,
   README. No operator-only paths in docs: no internal addresses, jump hosts, Pi daemon port, SNMP.
   The data itself is passed through as the site publishes it.

Another branch (`allow-unoffered`, not merged yet) adds a cached switches.json read and
`switch_port`/`boot_check` fields to `board_status`. Put the switches.json and machine.json reads
in `sites.py` as `welland_switches(live=False)` (TTL 60 s, parsed `list[dict]`) and
`welland_machine(serial, live=False)` (TTL 30 s); a conflict with that branch is resolved at merge.

## The contract: `GET /fleet/<serial>/machine.json`

Everything the device page `/fleet/<serial>/` shows, for programs (the page is for people and may
change its layout; this is the contract, as `/fleet/registry.json` is for the list). Same rules as
registry.json: times ISO 8601 with offset (UTC); a field the site does not know is `null`, never
guessed; keys are stable words, not the page's titles; marks (✓ ✗ ⟳) never appear, only result
words. 404 `{"error": "no host with this serial has registered"}` for an unknown serial.

```
{
  "site": "<site>", "generated_at": "<iso>", "serial": "<serial>",
  "page": "<absolute url of /fleet/<serial>/>",
  "machine": {
    "hostname", "label", "model", "model_full", "memory",            # as the page's head
    "port", "port_note",                                               # the port the gateway saw it on
    "condition": "operational" | "attention" | "missing" | "checking",
    "offered": bool, "offered_reason": null | "<why not offered>",
    "board_page": null | "<url of its /fpgas/ page when it has one>",
    "first_seen", "last_seen", "checked", "uptime_s" | null,
    "boot_id", "setup_port": bool, "cameras": [..] | null
  },
  "boards": [ {                                                        # this boot's boards
    "type", "variant", "id" | null, "label", "result", "reason",
    "tests": {"<name>": {"result": "<word>", "reason": "<text>"}},     # every check column
    "docs": null | "<url>" } ],
  "boot_checks": [ {                                                   # newest first, as the page
    "boot_id", "this_boot": bool, "at", "running": bool, "result", "reason",
    "boards": [ {"type", "result", "reason",
                 "tests": {"<name>": {"result", "reason"}}} ] } ],
  "labels": {
    "missing": [ {"label", "field", "sent_by", "sent_by_title"} ],
    "notes": ["<text>"], "refusals": [ {"kind", "text"} ],
    "images": [ {"title", "png", "pdf"} ], "label_input": "<url>" },
  "network": null | {
    "state", "administrative_state", "interface", "mac", "changed", "recorded",
    "last_ack", "last_ack_source", "expires", "boot_id", "current_boot",
    "addresses": [ {"family", "address", "scope", "valid_lifetime_s", "preferred_lifetime_s"} ],
    "default_routes": [ {..as the record has them..} ],
    "lease": null | { ..the DHCP lease fields the page shows.. } },
  "network_error": null | "<why the section cannot be shown>",
  "switch_port": null | { ..that port's row exactly as /management/switches.json has it.. },
  "registrations": [ {"fingerprint", "current": bool, "first_seen", "last_confirmed",
                      "document": { ..the registration document as sent.. }} ],
  "events": [ {"ts", "kind", "stage", "test", "result", "reason", "detail": { ..as sent.. } | null} ],
  "console": null | {"since", "receiving_since", "receiver_late": bool,
                     "lines": [ {"at", "text"} ]},
  "console_error": null | "<text>", "console_not_set_up": null | "<text>"
}
```

Query parameters (optional, for size): `events=<n>` keeps the newest n events, `console=<n>` the
newest n console lines (default: all, as the page). Unknown parameters are ignored.

The page and the JSON must come from the same code path (one function builds the data, the page
renders it, the JSON returns it), so they cannot drift.
