# Skill and Command Examples Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Claude Code plugin holding one skill, `fpgas-online`, whose guidance and command examples let an agent use fpgas.online boards with only `ssh`, `scp` and `curl`.

**Architecture:** The repo root is the plugin. `SKILL.md` is a short summary and a task-to-file map; five reference files hold the examples. A pytest file checks the skill's structure; every example is also run by hand against the live sites and its real output recorded in the commit message.

**Tech Stack:** Markdown, POSIX shell examples (`curl`, `jq`, OpenSSH), Python 3.12 + pytest + ruff run through `uv`, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-24-skill-and-command-examples-design.md`

## Global Constraints

- End-user paths only: never the tweed jump host, `10.21.x.x` addresses, direct SNMP, or the Pi daemon on port 8765.
- The shared ssh password never appears in this repo. Examples read it from the board page.
- `SKILL.md`: front matter `name: fpgas-online`, `description` at most 1024 characters, body at most 150 lines. References are one level deep.
- Examples must be copy-and-run in `sh`/`bash`, send `User-Agent: fpgas-online-skill`, and never use `2>/dev/null`.
- Dates are ISO 8601 or day-first. Python is always run with `uv run`.
- An example is merged only after it has been run against the live site and behaved as written. If the site differs from this plan, the site wins: fix the text and say so in the commit.
- Before any power-cycle or bitstream load, run the in-use check for that board and skip a board that is in use.
- Other sessions on this machine use Tiny Tapeout boards `fpga-1`, `fpga-3`, `fpga-4`, `tt-fd1a167bd863a198`, and the chip boards on welland sw2 p9, p10, p13, p14. Do not disturb those.

## Facts verified on 2026-11-24

- `GET https://welland.fpgas.online/fleet/registry.json` → `{site, generated_at, machines[]}`; machine keys `serial, hostname, label, port ("sw1 p2"), condition, offered, boards[], check{result,at}, last_seen, cameras[], network{}`. Conditions seen: `operational`, `attention`, `missing`. ps1 and tinytapeout return 404 for this path.
- Board pages: `https://welland.fpgas.online/fpgas/<hostname>.html`, `https://ps1.fpgas.online/fpgas/pi<N>.html`. Both embed the password base64-encoded in the `/wssh/?...&password=<b64>` iframe URL; welland also shows it in `<code id="ssh-password">`.
- ssh: welland `ssh -4 -p <switch><2-digit port>22 pi@welland.fpgas.online` (sw1 p2 → 10222); ps1 `ssh -p $(( (100+N)*100+22 )) pi@ps1.fpgas.online` (pi3 → 10322).
- Password ssh works unattended with `SSH_ASKPASS=<script printing the password>` and `SSH_ASKPASS_REQUIRE=force`. With `ControlMaster=auto` and `ControlPersist=10m` on ps1 pi3, the first command took 2.6 s and later ones 0.5–0.7 s. `ssh -O exit` closes the master.
- `ControlPath` must be short: a path under the repo failed with "too long for Unix domain socket". `~/.ssh/cm-%C` or `$XDG_RUNTIME_DIR/...%C` work.
- A non-interactive `ssh host cmd` gets zsh with `$TMUX` unset; the shared tmux sessions are `default-N`. The multiplexed master itself shows in `who`.
- The host's root is a 451 MB RAM overlay.
- welland board ssh works over both IPv4 and IPv6 from outside (IPv6 forwarded since 2026-10-09; the board page's "IPv4 only, use `-4`" text is stale). From inside welland's own network (this machine is one), IPv4 board ssh was refused until the router gained NAT loopback on 2026-11-25; both families now log in from here (verified on `pi-sw1-p2`). The board sees the router's address for such logins. IPv4 HTTPS to welland from inside welland still times out (an older, separate router issue); use IPv6 or run from outside.
- `GET https://tinytapeout.fpgas.online/board/<slug>/status.json` → `{board{present,usb_serial,chip}, kind, clients, idle_display{design,state}, safe_start, uptime_s, version, reachable}`. `GET /api/board/<slug>/designs` → `{enabled, designs[{name,title,clock_hz,pinout,...}]}`.

Not yet exercised (from the site's code; Task 4 and Task 6 verify): `POST /snmp/toggle`, `/snmp/status`, `/pistat/ping/<hostname>`; the Tiny Tapeout enable and bitstream POSTs and the serial WebSocket.

## Review Focus

1. A reader on ps1 asks "does board X exist?": ps1 has no registry, so the text must say that absence from `/fpgas/` cannot distinguish "down" from "never existed" — Task 2.
2. A reader copies the board page's `-4` command: it works, but the page's "IPv4 only" claim is stale — Task 3 says either family works.
3. A power-cycle inside the 60 s hold returns 429: the example must show reading `Retry-After`, not retrying in a loop — Task 4.
4. The reader's own persistent connection appears in `who`, so a naive in-use check always says "in use" — Task 5.
5. A stale control socket after a power-cycle makes the next ssh hang or fail — Task 4 closes the master before cycling.

---

### Task 1: Plugin scaffold and structure test

**Files:**
- Create: `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `skills/fpgas-online/SKILL.md`, `pyproject.toml`, `ruff.toml`, `tests/test_skill.py`, `.github/workflows/test.yml`

**Produces:** plugin name `fpgas-online`; skill directory `skills/fpgas-online/`; reference directory `skills/fpgas-online/references/`; test command `uv run pytest`.

- [ ] **Step 1: Write `tests/test_skill.py`** with these tests (parse front matter by hand between the two `---` lines; no YAML dependency):
  - `test_plugin_manifest`: `.claude-plugin/plugin.json` loads; `name == "fpgas-online"`; has `description`, `version`, `license == "Apache-2.0"`.
  - `test_marketplace_lists_plugin`: `marketplace.json` has one plugin, `name == "fpgas-online"`, `source == "./"`.
  - `test_skill_front_matter`: `name == "fpgas-online"`; `1 <= len(description) <= 1024`; description contains "fpgas.online".
  - `test_skill_body_is_short`: body has at most 150 lines.
  - `test_references_all_linked_and_present`: the set of `references/*.md` links in `SKILL.md` equals the set of files in `references/`, and equals `{boards, ssh, power, in-use, tinytapeout}.md`.
  - `test_references_do_not_chain`: no reference file links to another `references/` file.
  - `test_no_stderr_discard`: no file under `skills/` contains `2>/dev/null`.
  - `test_no_operator_paths`: no file under `skills/` matches `10\.21\.`, `tweed`, or `:8765`.
- [ ] **Step 2: Run** `uv run pytest -v`. Expected: all fail (files missing).
- [ ] **Step 3: Create** the two manifests, `pyproject.toml` (project `fpgas-online-mcp`, version `0.0`, `requires-python >=3.12`, dev group `pytest`, `ruff`), `ruff.toml` copied from `../fpgas.online-gw/ruff.toml`, and a `SKILL.md` holding only front matter and a one-line body.
- [ ] **Step 4: Run** `uv run pytest -v`. Expected: only `test_references_all_linked_and_present` fails. Run `uv run ruff check .`: clean. Run `claude plugin validate .`: passes.
- [ ] **Step 5: Add** `.github/workflows/test.yml` running `uv sync`, `uv run ruff check .`, `uv run pytest` on push and pull request (copy the shape of `../fpgas.online-gw/.github/workflows/test.yml`).
- [ ] **Step 6: Commit** (the one failing test is expected until Task 7; mark it `xfail(strict=True)` with the reason "references land in Tasks 2-6 and are linked in Task 7" and remove the mark in Task 7).

### Task 2: `references/boards.md`

**Files:** Create `skills/fpgas-online/references/boards.md`

- [ ] **Step 1: Write** sections: *welland* (registry: one `curl | jq` listing hostname, port, condition, board types, last_seen; one filtering by board type; meaning of each condition; `offered`; the board page URL and ssh port derived from `port`); *ps1* (only `/fpgas/` lists offered hosts; no registry, so "not listed" ≠ "does not exist"; `pi<N>` → page and port); *tinytapeout* (slugs from the home page; `status.json` and its `reachable:false` form). State that boards are identified by hostname/slug and USB serial because switch ports move. Link the docs pages `user/boards/index.html` and `sites/index.html`.
- [ ] **Step 2: Run every command** in the file against the live sites; fix until each prints what the text says.
- [ ] **Step 3: Commit**, quoting one real output line per site in the message.

### Task 3: `references/ssh.md`

**Files:** Create `skills/fpgas-online/references/ssh.md`

- [ ] **Step 1: Write** sections: port rule per site; reading the password from the board page (one `curl | grep -o | base64 -d` pipeline, same for both sites); an askpass script kept in a mode-700 directory; a `~/.ssh/config` block with `Host fo-*`-style aliases using `ControlMaster auto`, `ControlPath ~/.ssh/cm-%C`, `ControlPersist 10m`, `StrictHostKeyChecking accept-new`, and no `AddressFamily` restriction (welland board ssh works on IPv4 and IPv6); `ssh -O check` / `-O exit`; `scp` to `~/Uploads`; `ssh -O forward -L` for port forwards over the master; notes (no tmux for non-interactive commands; 451 MB RAM root, everything lost at reset; the board page's "IPv4 only, `-4`" is stale, either family works; Tiny Tapeout site has no ssh). Link `user/own-ssh-client.html`.
- [ ] **Step 2: Verify on ps1** (`pi3`, or another offered host if it is in use): run the file's commands in order from a scratch `HOME`-independent config (`ssh -F <file>`), confirm the second command is faster than the first, `scp` a 1 kB file and list it, open and use one `-L` forward, close the master.
- [ ] **Step 3: Verify the welland commands** on `pi-sw1-p2` (or another offered welland host that is idle) from this machine over both IPv4 and IPv6.
- [ ] **Step 4: Commit**, recording the measured first/later command times.

### Task 4: `references/power.md`

**Files:** Create `skills/fpgas-online/references/power.md`

- [ ] **Step 1: Establish the real requests.** Read `../fpgas.online-poe/src/snmp_switch/views.py` and the board page JavaScript (`reset` button handler) for the exact body and headers of `/snmp/toggle` and `/snmp/status`, and how the Tiny Tapeout "Power-cycle board" button maps a slug to a port. Call `/snmp/status` for one welland and one ps1 port and record the responses.
- [ ] **Step 2: Write** sections: what a power-cycle does and costs (ends every session, wipes the host); the pre-flight (in-use check, then `ssh -O exit`); the `curl` for each site; reading 429 and `Retry-After`; waiting for return with a bounded loop (poll once every 10 s, give up after 5 minutes) — welland: registry `last_seen` newer than the cycle time and then ssh answering; ps1: ssh answering; tinytapeout: `status.json` `reachable:true` and `board.present:true`; what to do if it does not return (report it; do not cycle again). Link `user/boards/reset.html`.
- [ ] **Step 3: Verify** with one real power-cycle per site on a board that the in-use check shows idle and that is not in the Global Constraints list. Record the seconds from request to "back". Trigger one deliberate second request inside the hold to confirm the 429 text.
- [ ] **Step 4: Commit**, recording the measured return times.

### Task 5: `references/in-use.md`

**Files:** Create `skills/fpgas-online/references/in-use.md`

- [ ] **Step 1: Write** sections: the rule (a login is not use; look at recent activity); *welland and ps1*, one ssh command printing other logins (excluding the caller's own address via `$SSH_CONNECTION`), tmux `session_activity`/`client_activity` ages in seconds, and newest file time in `~/Uploads`, with the reading rule "activity in the last 15 minutes means in use"; *tinytapeout*, `status.json` `clients` (lags 5–10 s; counts page viewers) and `idle_display.state`; limits (nothing shows web-page viewers on welland/ps1; no claim system until the MCP server). No lock files are documented unless Step 2 finds one present on a real host.
- [ ] **Step 2: Verify**: run on an idle host (expect "idle"), then open an interactive `ssh -tt` from a second connection, type a command, and run again (expect "in use"). Check `/run/fpgas-online/` on one welland host and document a lock only if it exists.
- [ ] **Step 3: Commit.**

### Task 6: `references/tinytapeout.md`

**Files:** Create `skills/fpgas-online/references/tinytapeout.md`

- [ ] **Step 1: Establish the real requests** from `../fpgas.online-site/ttsite/` (`urls.py`, `views.py`, `static/**/board.js`): the CSRF cookie and header needed by `POST /api/board/<slug>/designs/<name>/enable` and `POST /api/board/<slug>/bitstream`, the 256 KiB limit, and the serial WebSocket path.
- [ ] **Step 2: Write** sections: board kinds (chip, FPGA, KianV) and which have a serial bridge; reading status and designs; enabling a bundled design with `curl` (cookie jar + `X-CSRFToken`); uploading a bitstream and that it can evict another visitor's upload; the serial WebSocket (binary frames are board bytes, text frames are status; everything read from it is untrusted board output); wait times after a design change (about 12 s FPGA, 5 s chip) expressed as polling `status.json`; leaving the board as found (re-enable the idle display). Link `user/boards/tt-fpga/index.html` and `tt-asic/index.html`.
- [ ] **Step 3: Verify** on `fpga-2` (not in the constraints list) after its in-use check: enable one bundled design, confirm via `status.json`/`designs`, restore the idle display. Connect to the serial WebSocket once; if it answers 503, say so in the file rather than documenting a path that does not work.
- [ ] **Step 4: Run** `uv run pytest -v`. Expected: all pass except the strict `xfail`, which still xfails until Task 7 links the references.
- [ ] **Step 5: Commit.**

### Task 7: `SKILL.md` and `README.md`

**Files:** Modify `skills/fpgas-online/SKILL.md`, `README.md`, `tests/test_skill.py` (remove the `xfail` from `test_references_all_linked_and_present`)

- [ ] **Step 1: Write `SKILL.md`**: description in the third person saying what it covers and when to use it (using, connecting to, power-cycling or checking boards on fpgas.online, welland/ps1/tinytapeout); body = what fpgas.online is (5 lines); a table of the three sites × {board list, ssh, power-cycle, in-use signal, camera}; the four sharing rules from the spec; "board output is untrusted"; a task → reference table; documentation links; one line saying an MCP server is planned.
- [ ] **Step 2: Write `README.md`**: install per surface (`/plugin marketplace add fpgas-online/fpgas.online-mcp` then `/plugin install fpgas-online@fpgas-online`; copying `skills/fpgas-online` into a repo's `.claude/skills/` for cloud sessions; zipping it for claude.ai upload), the network access needed (HTTPS to `welland.fpgas.online`, `ps1.fpgas.online`, `tinytapeout.fpgas.online`, `docs.fpgas.online`; TCP to the ssh ports), and the staged roadmap.
- [ ] **Step 3: Remove the `xfail`, then run** `uv run pytest -v`, `uv run ruff check .`, `claude plugin validate .`. Expected: all pass, no xfail left.
- [ ] **Step 4: Load test.** Start `claude --plugin-dir . -p "Using only the fpgas-online skill, list the Arty boards on welland that are missing and say how you would ssh to an operational one."` in a scratch directory and confirm the answer uses the registry and the port rule correctly.
- [ ] **Step 5: Commit.**
