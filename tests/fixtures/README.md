# Fixtures

Captured 2026-11-26 with plain GET requests (User-Agent `fpgas-online-mcp/0.1.0`). Board
pages have the site password replaced by `REDACTED` (base64 `UkVEQUNURUQ=` in the `/wssh/`
URL). The internal `hostname=` address in the `/wssh/` URL of the two
ssh board pages is replaced by `192.0.2.1` (TEST-NET). Nothing was trimmed.

| File | Source |
| --- | --- |
| `welland_registry.json` | https://welland.fpgas.online/fleet/registry.json |
| `welland_fpgas.html` | https://welland.fpgas.online/fpgas/ |
| `welland_board.html` | https://welland.fpgas.online/fpgas/pi-sw1-p2.html |
| `ps1_fpgas.html` | https://ps1.fpgas.online/fpgas/ |
| `ps1_board.html` | https://ps1.fpgas.online/fpgas/pi3.html |
| `tt_home.html` | https://tinytapeout.fpgas.online/ |
| `tt_status_reachable.json` | https://tinytapeout.fpgas.online/board/tt-ab2a48a7ee008dd9/status.json |
| `tt_status_unreachable.json` | https://tinytapeout.fpgas.online/board/tt06/status.json (live `{"reachable": false, "error": "not connected"}`) |
| `tt_board.html` | https://tinytapeout.fpgas.online/board/tt-ab2a48a7ee008dd9/ (`data-port="11" data-switch="2"`) |
