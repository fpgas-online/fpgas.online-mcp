# Fixtures

Captured 2026-11-26 with plain GET requests (User-Agent `fpgas-online-mcp/0.1.0`). Board
pages have the site password replaced by `REDACTED` (base64 `UkVEQUNURUQ=` in the `/wssh/`
URL). The internal `hostname=` address in the `/wssh/` URL of the two
ssh board pages is replaced by `192.0.2.1` (TEST-NET). The two contact e-mail
addresses in `welland_fpgas.html` and `ps1_fpgas.html` are replaced by `user@example.org`.
Nothing was trimmed, except in the two files captured on 2026-11-27 (below).

Captured 2026-11-27, plain GETs, trimmed by a script to keep them small:
`welland_registry_conditions.json` keeps three machines of the live registry (one each
`operational`, `attention`, `missing`; it has the `setup_port` field the older capture lacks);
`welland_switches.json` keeps switch 1 ports 1, 2, 10 and switch 2 ports 11, 38, and of each
port only `port`, `link_up`, `poe_state`, `watts`, `can_power` and a few fields of its machines.

| File | Source |
| --- | --- |
| `welland_registry.json` | https://welland.fpgas.online/fleet/registry.json |
| `welland_fpgas.html` | https://welland.fpgas.online/fpgas/ |
| `welland_board.html` | https://welland.fpgas.online/fpgas/pi-sw1-p2.html |
| `welland_registry_conditions.json` | https://welland.fpgas.online/fleet/registry.json (trimmed) |
| `welland_switches.json` | https://welland.fpgas.online/management/switches.json (trimmed) |
| `ps1_fpgas.html` | https://ps1.fpgas.online/fpgas/ |
| `ps1_board.html` | https://ps1.fpgas.online/fpgas/pi3.html |
| `tt_home.html` | https://tinytapeout.fpgas.online/ |
| `tt_status_reachable.json` | https://tinytapeout.fpgas.online/board/tt-ab2a48a7ee008dd9/status.json |
| `tt_status_unreachable.json` | https://tinytapeout.fpgas.online/board/tt06/status.json (live `{"reachable": false, "error": "not connected"}`) |
| `tt_board.html` | https://tinytapeout.fpgas.online/board/tt-ab2a48a7ee008dd9/ (`data-port="11" data-switch="2"`) |
