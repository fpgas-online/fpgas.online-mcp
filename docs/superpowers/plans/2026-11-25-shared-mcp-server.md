# Shared MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-machine broker plus a per-session stdio MCP front end (and a CLI) that share claims, a cache and ssh masters for fpgas.online boards.

**Architecture:** `fpgas-online-broker` (asyncio, Unix socket, newline-delimited JSON) owns all state and site/ssh access. `fpgas-online-mcp` (one per Claude session, stdio, official `mcp` SDK) and `fpgas-online` (CLI) are thin clients that start the broker on demand. Everything is unit-tested with a mocked HTTP transport and a fake `ssh` executable; one live smoke script runs before merge.

**Tech Stack:** Python ≥3.12, `uv`, `mcp==2.3.0`, `httpx==0.28.1`, pytest + pytest-asyncio, ruff.

**Spec:** `docs/superpowers/specs/2026-11-25-shared-mcp-server-design.md`

## Global Constraints

- Pins exactly: `mcp==2.3.0`, `httpx==0.28.1`. Before Task 9, read the `mcp` 2.3.0 server docs/source for the current tool-registration, annotations and in-memory client API — do not rely on memory of 1.x.
- End-user paths only: never tweed, `10.21.x.x`/`10.99.x.x` addresses, SNMP, the Pi daemon port 8765. The tests from stage 1 keep enforcing this on `skills/`; add the same check for `src/`.
- Passwords never in the repo, tests, fixtures, logs or tool output. Fixtures of board pages have the password replaced by `REDACTED` (base64 `UkVEQUNURUQ=`).
- Every HTTP request sends `User-Agent: fpgas-online-mcp/<version>`.
- Never `2>/dev/null` in shipped shell; never open interactive ssh (`-t`/`-tt`).
- "unknown" is never reported as "idle". Any failure to read a signal → `unknown`.
- Directories: runtime `$XDG_RUNTIME_DIR/fpgas-online/` (fallback `$XDG_CACHE_HOME/fpgas-online/run/`), state `$XDG_STATE_HOME/fpgas-online/`, cache `$XDG_CACHE_HOME/fpgas-online/`, config `$XDG_CONFIG_HOME/fpgas-online/` (same dir the stage 1 skill uses for `<site>.pw`). All created mode 0700; secret files 0600.
- Board id format `<site>:<name>`, sites `welland`, `ps1`, `tinytapeout`. ssh aliases `fpgas-<site>-<name>`.
- Claims: default 30 min, max 240 min. Cache TTLs: welland registry 60 s, ps1 `/fpgas/` 60 s, tinytapeout home 300 s, `status.json` 10 s, board pages 86400 s. In-use and power paths never read the cache.
- Tool output default ≤ 4 KB; `run` output ≤ 8 KB (head 4 KB + tail 4 KB), board text wrapped in `<board-output untrusted="true">…</board-output>`.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Dates ISO 8601. `uv run` for all Python.
- Do not change any board in tests. Live steps follow the stage 1 rules: in-use check first; never touch Tiny Tapeout `fpga-1`, `fpga-3`, `fpga-4`, `tt-fd1a167bd863a198`, welland sw2 p9/p10/p13/p14, ps1 `pi3`/`pi7`.

## Review Focus

1. A broker that crashed leaves a stale socket file: the next client gets ECONNREFUSED and must remove it (under the start lock) and start a new broker, not hang or fail — Task 2.
2. A site is down or returns garbage: inventory serves stale cache marked `stale: true` with its age; a parse failure on fresh data never empties the board list — Task 3.
3. Board output with invalid UTF-8 or terminal escape codes: decoded with `errors="replace"`, escapes stripped, then wrapped — Task 6.
4. Two sessions claim the same board in the same instant: exactly one wins (claims mutate under one asyncio lock and are written atomically via rename) — Task 5.
5. A session's front end restarts mid-claim: the claim is still usable by passing its `claim_id`; `list_boards` shows the id to its holder label — Tasks 5 and 9.

---

### Task 0: Correct the stage 1 skill's ps1 power text

**Files:** Modify `skills/fpgas-online/SKILL.md`, `skills/fpgas-online/references/power.md`

- [ ] **Step 1:** Replace "the host-to-port link is unverified (a toggle may hit another board): ask the user first" (SKILL.md table) and the matching power.md text with the finding in the spec's "Why the ps1 pi21 cycle did not reboot it" bullet: the switch does cycle the named port; several ps1 hosts have no PoE HAT and may not lose power, so check the boot id and, if unchanged, tell the user the host must be reset by hand. Keep "no hold on ps1".
- [ ] **Step 2:** `uv run pytest -v` → 10 passed. Commit.

### Task 1: Package skeleton and paths

**Files:** Modify `pyproject.toml`; Create `src/fpgas_online_mcp/__init__.py` (`__version__`), `src/fpgas_online_mcp/paths.py`, `tests/test_paths.py`; Modify `tests/test_skill.py` (extend forbidden-string scan to `src/`).

**Produces:** `paths.runtime_dir() -> Path`, `state_dir()`, `cache_dir()`, `config_dir()` (each creates the dir 0700 and returns it); `__version__ = "0.1.0"`; console scripts `fpgas-online-mcp = fpgas_online_mcp.mcp_server:main`, `fpgas-online-broker = fpgas_online_mcp.broker:main`, `fpgas-online = fpgas_online_mcp.cli:main` (stub `main` functions raising `SystemExit(2)` until their task).

- [ ] **Step 1: Tests** `test_runtime_dir_uses_xdg_runtime`, `test_runtime_dir_fallback_without_xdg_runtime` (monkeypatch env, tmp_path), `test_dirs_are_mode_0700` (also when the dir pre-exists with 0755 — it is tightened), `test_socket_path_short` (`runtime_dir()/"broker.sock"` under 100 bytes for a typical `$XDG_RUNTIME_DIR=/run/user/1000`).
- [ ] **Step 2:** Run → fail. **Step 3:** Implement; add deps and dev deps (`pytest-asyncio`), hatchling build with `src/` layout. **Step 4:** `uv run pytest -v`, `uv run ruff check .` → pass. Commit.

### Task 2: Broker socket, protocol, start-up and lifecycle

**Files:** Create `src/fpgas_online_mcp/protocol.py`, `broker.py`, `client.py`, `tests/test_broker.py`.

**Produces:**
- `protocol.encode(obj: dict) -> bytes` (JSON + `\n`), `protocol.decode(line: bytes) -> dict`. Request `{"id": int, "op": str, "args": dict, "version": str, "holder": str}`; response `{"id": int, "ok": true, "result": …}` or `{"id": int, "ok": false, "error": {"code": str, "message": str}}`.
- `broker.Broker(clock=time.monotonic)` with `register(op: str, handler: Callable[[dict, Conn], Awaitable[Any]])`; built-in ops `ping` → `{"version", "pid", "started"}`, `shutdown_if_idle` → refuses (`code="busy"`) while claims exist; `async broker.serve(sock_path: Path, broker: Broker, idle_exit_s: float = 1800)`; `main()`.
- `client.BrokerClient.call(op: str, **args) -> Any` (raises `BrokerError(code, message)`); `async client.connect(holder: str, spawn: bool = True) -> BrokerClient` — connect; on missing socket or ECONNREFUSED take `flock(runtime_dir()/"broker.lock")`, re-check, remove a stale socket, spawn `fpgas-online-broker` with `start_new_session=True` and stdio to `state_dir()/"broker.log"`, wait ≤5 s; on version mismatch call `shutdown_if_idle` then reconnect, or raise `BrokerError("version", …)`.
- Peer check: reject connections whose `SO_PEERCRED` uid ≠ `os.getuid()`.

- [ ] **Step 1: Tests** (run the broker in-process on a tmp socket; spawn tests use a real subprocess with `XDG_RUNTIME_DIR=tmp_path`): `test_ping_roundtrip`, `test_unknown_op_error_result`, `test_two_clients_spawn_one_broker` (two `connect()` concurrently → same broker pid), `test_stale_socket_replaced`, `test_version_mismatch_restarts_when_idle`, `test_version_mismatch_refused_with_claims`, `test_idle_exit` (idle_exit_s=0.2 → serve returns), `test_socket_mode_0600`.
- [ ] **Steps 2-4:** fail → implement → pass (+ruff). Commit.

### Task 3: Site client and cache

**Files:** Create `src/fpgas_online_mcp/cache.py`, `sites.py`, `tests/fixtures/` (captured once, live, GET only, passwords redacted: welland `registry.json`, welland `/fpgas/` and one board page, ps1 `/fpgas/` and `pi21.html`, tinytapeout home, one `status.json` reachable and one `{"reachable": false, …}`, one TT board page), `tests/test_sites.py`.

**Produces:**
- `cache.Entry(data: bytes, as_of: datetime, stale: bool)`; `cache.Cache(dir: Path, clock)`; `async Cache.get(key: str, ttl_s: float, fetch: Callable[[], Awaitable[bytes]], live: bool = False) -> Entry` — single-flight per key, persisted under `dir`, stale served when `fetch` raises and an old entry exists, `live=True` bypasses reads.
- `sites.SiteClient(http: httpx.AsyncClient, cache: Cache)` with `async welland_registry() -> (list[dict], Entry)`, `ps1_hosts() -> (list[str], Entry)`, `tt_slugs() -> (list[str], Entry)`, `tt_status(slug, live=False) -> (dict, Entry)`, `board_page(site, name) -> BoardPage(password: str | None, ssh_port: int | None, tt_port: str | None, tt_switch: str | None)`, `toggle(site, port: str, switch: int | None) -> ToggleResult(status: int, body: dict, retry_after: int | None)`.
- Password extraction: base64 from the `/wssh/?...&password=` URL; returned value never logged.

- [ ] **Step 1: Tests** with `httpx.MockTransport` serving the fixtures: parse each page; `test_cache_ttl`, `test_single_flight` (10 concurrent gets → 1 fetch), `test_stale_on_error`, `test_live_bypasses_cache`, `test_parse_failure_keeps_previous` (garbage fresh body → previous entry served `stale=True`), `test_user_agent_header`, `test_toggle_429_retry_after`, `test_ps1_port_sent_as_string`.
- [ ] **Steps 2-4.** Commit (fixtures in their own commit after checking `git diff` shows no password).

### Task 4: Board identity and inventory

**Files:** Create `src/fpgas_online_mcp/boards.py`, `tests/test_boards.py`.

**Consumes:** `SiteClient` (Task 3). **Produces:** `BoardId.parse(s: str, known: list[BoardId]) -> BoardId` (accepts `site:name` or an unambiguous bare name; raises `ValueError` listing candidates); `Board(id, site, name, type: str | None, condition: str, offered: bool | None, last_seen: str | None, boot_id: str | None, usb_serial: str | None, as_of: str, stale: bool)`; `async inventory(sites, site: str | None = None) -> Inventory(boards: list[Board], notes: dict[site, str])` where notes state completeness (welland "every registered machine", ps1 "only hosts offered now; absence says nothing", tinytapeout "catalogue; condition from status.json").

- [ ] **Step 1: Tests:** welland duplicate hostname (missing + live) → one board, the live one, `condition` from it; ps1 conditions `offered`; tinytapeout `reachable:false` → `condition="not connected"`; bare-name ambiguity error text; site filter. **Steps 2-4.** Commit.

### Task 5: Claims

**Files:** Create `src/fpgas_online_mcp/claims.py`, `tests/test_claims.py`.

**Produces:** `Claim(claim_id: str, board: str, holder: str, purpose: str, started: datetime, expires: datetime, override_reason: str | None, holder_connected: bool)`; `ClaimStore(dir: Path, clock)`: `claim(board, minutes, purpose, holder, claim_id=None, override_reason=None) -> Claim` (renew when `claim_id` matches; `ClaimConflict(existing: Claim)` otherwise when a live claim exists), `release(claim_id) -> None`, `get(claim_id) -> Claim | None`, `for_board(board) -> Claim | None`, `all() -> list[Claim]`, `set_connected(holder, connected: bool)`, `active_count() -> int`. Files `dir/claims/<site>_<name>.json`, written via temp file + `os.replace`; expired claims dropped on read. The in-use refusal is applied by the caller (Task 9), not here.

- [ ] **Step 1: Tests:** default 30 / max 240 (241 → `ValueError`), renew extends, conflict, release, expiry with a fake clock, survives a new `ClaimStore` on the same dir, concurrent claims (two tasks via `asyncio.gather` under the store's lock → one `ClaimConflict`), disconnected holder still holds. **Steps 2-4.** Commit.

### Task 6: ssh layer

**Files:** Create `src/fpgas_online_mcp/ssh.py`, `tests/fake_ssh.py` (an executable script the tests put first on `PATH`: records argv to a log, emulates `-O check`/`-O exit`/`-MNf`, runs the remote command locally with `sh -c` for `run`), `tests/test_ssh.py`.

**Consumes:** `paths`, `SiteClient.board_page` (password, port). **Produces:** `SshManager(sites, ssh_bin="ssh")`: `alias(board) -> str`, `async ensure_master(board) -> MasterInfo(alias, config_path, ssh_line, scp_line, include_line)`, `async check(board) -> bool`, `async exit(board) -> None`, `async run(board, command: str, timeout_s: float) -> RunResult(exit_status: int | None, output: str, truncated: bool, timed_out: bool)`; module function `wrap_untrusted(text: str) -> str`. Writes `config_dir()/ssh_config` (one `Host` block per board; options from the spec), `config_dir()/<site>.pw` (0600) and `config_dir()/askpass` (0700, picks the file by the host in the prompt — same logic as stage 1 ssh.md). Never passes `-t`/`-tt`; adds `-o ClearAllForwardings=yes`.

- [ ] **Step 1: Tests:** config content and modes; `ensure_master` is idempotent (second call → only `-O check` in the log); `run` truncation head/tail at 8 KB; invalid UTF-8 and ANSI escapes cleaned; timeout kills the process and sets `timed_out`; no argv ever contains `-t`; password never appears in the fake ssh log or in any returned string. **Steps 2-4.** Commit.

### Task 7: In-use check

**Files:** Create `src/fpgas_online_mcp/inuse.py`, `src/fpgas_online_mcp/data/in_use.sh`, `tests/test_inuse.py`.

**Consumes:** `SshManager.run`, `SiteClient.tt_status(live=True)`, `Claim`. **Produces:** `Verdict(verdict: Literal["idle","in use","unknown"], signals: list[str], as_of: str)`; `async check(board: Board, ssh, sites, claim: Claim | None) -> Verdict`. `data/in_use.sh` is the remote script from `references/in-use.md`, verbatim; a test extracts the fenced block from in-use.md and asserts equality. Holder discounting exactly as the spec's "claim holder's own activity" paragraph; the claim-time TT `status.json` snapshot is stored on the claim (add `tt_state_at_claim: str | None` to `Claim` in this task).

- [ ] **Step 1: Tests:** script parity; parse each verdict line form; TT rule (`clients` 0 twice ≥15 s apart with a fake clock and `loaded` → idle; `waiting` → re-read twice then `in use`; chip board → `unknown`); holder discount (`~/Uploads` newer than claim start ignored; another login not ignored); any exception → `unknown`. **Steps 2-4.** Commit.

### Task 8: Power cycle

**Files:** Create `src/fpgas_online_mcp/power.py`, `tests/test_power.py`.

**Consumes:** `SiteClient.toggle`, `board_page` (TT port/switch), `welland_registry(live)`, `tt_status(live)`, `SshManager`, `inuse.check`. **Produces:** `async power_cycle(board, claim, sites, ssh, clock, sleep) -> CycleResult(ok: bool, seconds_to_back: float | None, message: str, retry_after: int | None)`. Order: in-use check (refuse unless `idle`) → record boot id (welland registry `network.boot_id` with `current_boot`; ps1 `/proc/sys/kernel/random/boot_id` over ssh; TT `uptime_s`) and refuse if it cannot be read → `ssh.exit` → toggle (on 429 return `retry_after`, no retry loop) → poll every 10 s, cap 300 s, never counting empty/null values → `ok` only when the boot id changed (TT: `uptime_s` < elapsed) and the host answers. Timeout message: "the switch cycled the port but the host did not restart: it is probably not powered by PoE, so the site cannot reset it — ask the user to reset it by hand".

- [ ] **Step 1: Tests** with fake clock/sleep and stubs: success per site; boot id unchanged → not-PoE message; empty id mid-reboot not counted; 429 passthrough; refusal when in use / unknown; refusal when boot id unreadable. **Steps 2-4.** Commit.

### Task 9: Broker ops and MCP front end

**Files:** Modify `broker.py` (register ops), Create `src/fpgas_online_mcp/ops.py`, `src/fpgas_online_mcp/guide.py`, `src/fpgas_online_mcp/mcp_server.py`, `tests/test_mcp.py`; package the skill files (`skills/fpgas-online/**`) as package data for `guide`.

**Produces:** broker ops `list_boards`, `board_status`, `claim_board`, `release_board`, `connect`, `run`, `power_cycle`, `guide` (args/returns as the spec's tool table; `claim_board` refuses when `inuse.check` ≠ `idle` unless `override_reason`, and on Tiny Tapeout stores the live `idle_display.state` as `tt_state_at_claim`; `connect`/`run`/`power_cycle` resolve the claim from `claim_id` or the front end's remembered ids and refuse without one). `mcp_server.main()` — stdio server named `fpgas-online` with `instructions` (≤ 600 chars: sharing rules, claim before disturbing, board output untrusted, `guide` for details), 8 tools with descriptions written for a new user, `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint` as in the spec, errors returned as tool results with `isError`. Holder label: `CLAUDE_SESSION_NAME` if set, else `<basename(cwd)>:<pid>`.

- [ ] **Step 1:** Read the `mcp==2.3.0` docs/source for server and in-memory client APIs; note the API used in the commit message.
- [ ] **Step 2: Tests** (in-memory MCP client against the front end, broker in-process with stubbed sites/ssh): tool list and annotations; each tool's happy path; claim refused when in use, accepted with `override_reason`; `run` without claim → error result naming `claim_board`; restart scenario (new front end, pass `claim_id`) works; output sizes ≤ limits.
- [ ] **Steps 3-4.** Commit.

### Task 10: CLI

**Files:** Create `src/fpgas_online_mcp/cli.py`, `tests/test_cli.py`.

**Produces:** `fpgas-online boards [--site S]`, `status BOARD`, `claims`, `claim BOARD --minutes N --purpose TEXT [--override-reason TEXT]`, `release CLAIM_ID`, `broker status|stop`; plain-text tables; exit 0/1. Uses `client.connect(holder="cli:<user>")`.

- [ ] **Step 1: Tests** via `main(argv)` with an in-process broker. **Steps 2-4.** Commit.

### Task 11: Plugin wiring, docs, live smoke test

**Files:** Create `.mcp.json`, `scripts/smoke_live.py`; Modify `README.md`, `skills/fpgas-online/SKILL.md` (one line: when the `fpgas-online` MCP tools are present, prefer them; skill-only agents can see claims with `fpgas-online claims` or in the claims directory), `.claude-plugin/plugin.json` (version 0.1.0).

- [ ] **Step 1:** `.mcp.json`: `{"mcpServers": {"fpgas-online": {"command": "uv", "args": ["run", "--project", "${CLAUDE_PLUGIN_ROOT}", "fpgas-online-mcp"]}}}`. README: install (plugin; repo-level `uvx --from git+https://github.com/fpgas-online/fpgas.online-mcp@v0.1 fpgas-online-mcp` entry), CLI, where state lives, stopping the broker, sandbox note.
- [ ] **Step 2:** `scripts/smoke_live.py`: read-only by default (inventory all sites, `board_status` with in-use on welland `pi-sw1-p2` and ps1 `pi21`, claim/connect/run `hostname`/release); `--power welland:pi-sw1-p2` additionally power-cycles that board after its in-use check.
- [ ] **Step 3:** Run it read-only, then with `--power welland:pi-sw1-p2`; record outputs and timings in the commit message. `claude plugin validate .`; `claude --plugin-dir . -p "List the boards on welland that are missing, using the fpgas-online tools."` from a scratch dir and confirm the tools were called.
- [ ] **Step 4:** `uv run pytest -v`, ruff. Commit.
