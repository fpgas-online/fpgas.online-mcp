# Stage 2 design: a local MCP server shared by the sessions on one machine

Date: 2026-11-25. Status: approved by Tim 2026-11-25.

## Intent

Stage 1 gave agents a skill: guidance plus `curl`/`jq`/`ssh` examples. Stage 2 adds an MCP
server for what a skill cannot do well: state that outlives one command and is shared
between the agent sessions running on one machine. Today the Tiny Tapeout sessions on
big-storage coordinate boards by hand (claim files, cross-session messages), re-fetch the
same board lists, and re-open ssh tunnels that drop. One shared server fixes that, and lets
an agent answer "does this board exist, is it up, is someone on it, can I have it?" in one
call.

It is for people and agents who use the boards, not for running the infrastructure.

## Goals

1. **Guidance.** Short server `instructions` plus one tool that returns the summary and doc
   links for a topic, taken from the stage 1 skill text (one source of truth).
2. **Persistent ssh** to a board's host using standard OpenSSH multiplexing
   (`ControlMaster`/`ControlPath`/`ControlPersist`), so later commands are fast. The session
   can use the same connection from its own shell with plain `ssh`.
3. **Power-cycle and wait** through the site's own endpoint, until the host has really
   rebooted (boot id changed) and ssh (or `status.json`) answers. No switch or SNMP access.
4. **Board inventory**: every board that exists, including offline ones, with its condition,
   so "down" and "never existed" are distinguishable where the site allows it (welland's
   registry; ps1 and tinytapeout have weaker lists — say so in results).
5. **In use**: the stage 1 in-use check, run through the server, combined with local claims.
6. **One server per machine** that every local session connects to, holding the claims, the
   cache and the ssh connections.
7. **Local cache** so N sessions asking cost the sites one request.
8. Works in local Claude Code and in Claude Code cloud sessions (each cloud VM runs its own
   server; there is nothing to share there).

## Non-goals

- Tiny Tapeout design enable, bitstream upload and serial bridge tools (stage 1 documents
  them; tools can come later once they are exercised live).
- A hosted endpoint for claude.ai (stage 3).
- Enforcing claims against people on the website: claims are local and advisory. When
  fpgas.online-api gains leases, the server should use them instead.
- Anything that needs operator access (tweed, `10.21.x.x`, SNMP, the Pi daemon port).

## Anti-goals

- An agent disturbing a human because the server said "idle". Every uncertain answer is
  "in use" or "unknown", never "idle".
- A tool that runs arbitrary commands without the caller holding a claim.
- Long tool outputs. Default results fit in a few KB; board output is truncated and marked
  untrusted.

## Constraints and facts (from stage 1, verified live 2026-11-24/25)

- All site interfaces are public, no login. welland `/fleet/registry.json` lists every
  registered machine with `condition` (operational/attention/missing/checking), `offered`,
  `last_seen` and `network.boot_id`/`current_boot`. ps1 has only `/fpgas/` (offered hosts).
  tinytapeout has the home page and `/board/<slug>/status.json`.
- ssh: user `pi`, one password per site, read from the board page. Ports: welland
  `<switch><2-digit port>22`, ps1 `(100+N)*100+22`. Non-interactive ssh never joins the shared
  tmux; interactive logins must never be opened.
- Power: `POST /snmp/toggle` (JSON, `Content-Type: application/json`); welland/tinytapeout
  have a 60 s hold (429 + `Retry-After`); ps1 has no hold.
- **Why the ps1 `pi21` cycle did not reboot it** (investigated 2026-11-25, read-only):
  ps1 runs `snmp_switch` 0.0.17, whose toggle sets the port's PoE off, waits 0.5 s, sets it
  on, and returns the states the switch echoed back — so `["off","on"]` means the switch
  accepted both on port 21 (e21, which the site notes give to pi21). pi21 is a Raspberry
  Pi 5 with no PoE HAT, running from a 5 V USB-C supply without USB-PD (`max_current`
  900 mA); its undervoltage flag was never set, its Ethernet link never dropped since it
  booted on 2026-11-17, and its boot id did not change. So e21's PoE does not power pi21:
  the cycle reached the right port and the host simply does not depend on it. No other
  reachable ps1 host rebooted at the time (pi3 booted 2026-11-24 00:46, pi20 2026-10-23;
  pi7 booted 16:09 UTC, well after the test, and is used by another session). Other ps1
  hosts checked also have no PoE HAT (pi3: Pi 4; pi7: Pi 3B+ with a Pmod HAT adaptor), so a
  ps1 Reset may be a no-op for them too unless they are fed by a PoE splitter.
- Consequence: no ps1-specific confirmation. `power_cycle` treats ps1 like the other sites
  (claim + fresh in-use check), and when the boot id does not change it reports "the switch
  cycled the port but this host did not lose power: it is probably not powered by PoE, so
  the site cannot reset it — ask the user to reset it by hand", rather than a generic
  timeout. The stage 1 skill's "a ps1 toggle may hit another board: ask the user first"
  text is corrected to match (first task of the plan).
- The in-use logic and its limits are in `skills/fpgas-online/references/in-use.md`; the
  Tiny Tapeout idle rule (`clients` 0 twice ≥15 s apart and `idle_display.state == loaded`;
  chip boards: `clients` 0 twice) is there too. Since 2026-11-27 (2026-11-27-tt-boards-over-ssh-design.md) a Tiny Tapeout
  board is also checked over ssh on the welland Pi it is plugged into.

## Approaches

1. **Background broker + per-session MCP front end over a private socket** (chosen by Tim,
   2026-11-25). Each Claude session's plugin starts a small stdio MCP server; it connects to
   one long-running broker process on a Unix socket, starting it if needed. The broker owns
   claims, cache, ssh masters and site calls.
2. Local Streamable-HTTP MCP server on a port that sessions connect to by URL: started
   separately, port clashes, localhost auth/Origin checks, unusable from cloud sessions.
3. No shared process: each session runs its own server and coordinates through lock files.
   Simplest, but not one shared server, and no single view of all claims.

## Design

### Processes

- **`fpgas-online-mcp`** (one per Claude session, stdio): the MCP server Claude talks to.
  Holds no state. Each tool call becomes one request to the broker. It identifies its
  session to the broker once (see Holders).
- **`fpgas-online-broker`** (one per user per machine): a plain asyncio service on
  `$XDG_RUNTIME_DIR/fpgas-online/broker.sock` (fallback `~/.cache/fpgas-online/run/` when
  `XDG_RUNTIME_DIR` is unset). Directory mode 0700, socket 0600, and it checks the peer's uid
  (`SO_PEERCRED`). Protocol: newline-delimited JSON requests/responses with a `version`
  field. It is not itself an MCP server, so MCP protocol changes touch only the front end.
- **`fpgas-online`** CLI: the same broker requests from a shell (`fpgas-online boards`,
  `status`, `claims`, `claim`, `release`), for humans and for agents using only the skill.
- **Start-up**: the front end connects; if there is no socket it takes an `flock` on
  `broker.lock`, starts the broker detached (`setsid`), and waits up to 5 s for the socket.
  Two sessions starting at once therefore start one broker.
- **Version**: the broker reports its package version and an integer `protocol` (1 now; it
  changes only when the wire format does). Only a client newer than the broker (versions
  compared as tuples of ints) asks it to restart, which it does only when it holds no active
  claims, no other client is connected and no other request is in flight; while it is busy,
  and whenever the client is older, the client uses the running broker. Only a different
  `protocol` is an error. So two versions running side by side never take turns replacing
  each other's broker. The broker exits after 30 minutes with no connected front ends and no
  claims (ssh masters keep their own `ControlPersist` lifetime).

### Board identity

A board is `<site>:<name>`: `welland:pi-sw1-p2`, `ps1:pi3`, `tinytapeout:fpga-4`. Tools
accept that id (and a bare name when unambiguous). Results also carry the facts that
survive port moves where the site gives them: USB serial, board type, FPGA part.

### Holders and claims

- A claim belongs to its `claim_id`, not to a process. The holder label
  (`<label>@<host>`: the session's name if the environment exposes one, otherwise its
  working directory and front-end pid) is only for display.
- `claim(board, minutes, purpose)` returns a `claim_id`. Default 30 minutes, maximum 240,
  renewable by claiming again with the same id. Claims are advisory and local to the machine.
- `connect`, `run` and `power_cycle` take an optional `claim_id`. The front end remembers
  the ids it created and fills them in; after a restart (`/mcp` reconnect, `--resume`,
  broker restart) the session passes the id it was given, which `list_boards` also shows.
- A claim whose front end has disconnected stays held until it expires, shown as
  "holder disconnected". There is no shorter grace period: a session that stops for lunch
  keeps its board until its own expiry.
- Claims are also written as one JSON file per board in
  `$XDG_STATE_HOME/fpgas-online/claims/` (holder, purpose, expiry), so the CLI and
  skill-only agents see them, and they survive a broker restart.
- A claim is refused when another live claim exists, or when the board's in-use check says
  "in use"/"unknown" — unless the caller passes `override_reason`, which is recorded in the
  claim and shown to others.
- One claim per Pi (2026-11-27, 2026-11-27-tt-boards-over-ssh-design.md): a Tiny Tapeout board's claim records the welland
  Pi it was plugged into when claimed (`host`), and a claim on the board and one on that Pi
  exclude each other. If the board moves, `connect`, `run` and `power_cycle` refuse
  (`host_changed`) until the board is claimed again; claims are never re-keyed.

### Tools (8)

| Tool | What it does | Annotations |
| --- | --- | --- |
| `guide(topic?)` | Summary of what fpgas.online offers and the sharing rules; with a topic (`boards`, `ssh`, `power`, `in-use`, `tinytapeout`), that reference's key points plus the docs links. Text comes from the skill files shipped in the package. | read-only |
| `list_boards(site?, condition?, board_type?, include_offline=true)` | Merged inventory from cache, one dense row per board `[id, type, condition, offered, last_seen]`, local claims by board id (holder, expiry), the conditions present, and `as_of` per site. Says per site how complete the list is. Its limit is 8 KB, so all three sites fit uncut. | read-only |
| `board_status(board, check_use=true)` | One board in detail: inventory facts, local claim, and (if `check_use`) a live in-use check with its signals. | read-only |
| `claim_board(board, minutes=30, purpose, override_reason?)` | Take or renew a claim (above). | not read-only, not destructive |
| `release_board(claim_id)` | Release. | not read-only |
| `connect(board, claim_id?)` | Ensure an ssh master to the host (needs the caller's claim); returns the alias, the generated config path, and the exact `ssh -F … <alias> 'cmd'` / `scp -F …` lines to use from the shell. | not read-only, not destructive |
| `run(board, command, claim_id?, timeout_s=60)` | Run one non-interactive command over the master. Requires the caller's claim. Returns exit status and output truncated to 8 KB (head and tail), wrapped as untrusted board output. | not read-only, destructive=false, open-world |
| `power_cycle(board, claim_id?)` | Requires the caller's claim and a fresh in-use check (any signal other than the caller's own → refuse). Closes the master, records the boot id (or Tiny Tapeout `uptime_s`), toggles, honours `Retry-After`, waits until the boot id changes and ssh/status answers (cap 5 minutes), and reports timings. If the boot id never changes, says the host is probably not PoE-powered (see ps1 above) instead of a bare timeout. | destructive, not idempotent |

Errors are returned as tool results that say what to do next (for example "claimed by
X until 15:40; ask the user or pick another board").

### ssh

- There is one master per board, shared by every local session and the broker. Commands
  through it all belong to one login on the host, so the in-use script cannot see one local
  session's work from another: the local claim is the only guard between local sessions.
  That is why `connect` requires a claim.
- Aliases are `fpgas-<site>-<host>` (not stage 1's `fo-…`), so a stage 1 `Host fo-*` block
  in `~/.ssh/config` cannot collide with the broker's config and open a second master.
- The broker writes `~/.config/fpgas-online/ssh_config` with one `Host fpgas-<site>-<host>`
  block per board it has connected to (HostName, Port, User `pi`, `ControlMaster auto`,
  `ControlPath <runtime dir>/cm-%C`, `ControlPersist 30m`, `StrictHostKeyChecking
  accept-new`, `UserKnownHostsFile ~/.config/fpgas-online/known_hosts`), and an askpass
  script reading the per-site password file (0600) — the stage 1 layout. It never edits
  `~/.ssh/config`; `connect` tells the user the one `Include` line to add if they want
  plain `ssh fo-…` to work.
- Masters are started with `ssh -F <config> -MNf <alias>`; status with `-O check`; closed
  with `-O exit` before a power-cycle.
- A Tiny Tapeout board has no master of its own: `connect` and `run` use the master of the
  welland Pi its live board page names (`data-switch`/`data-port`), and say which (`host`).
  A Pi that carries only a chip board is not listed by welland (its page is a 404), so its
  password is the site-wide one from a listed Pi's page (2026-11-27, 2026-11-27-tt-boards-over-ssh-design.md).

### Cache

- Files in `$XDG_CACHE_HOME/fpgas-online/` with the fetch time. TTLs: welland registry 60 s,
  ps1 `/fpgas/` 60 s, tinytapeout home page 300 s, `status.json` 10 s, board pages (for
  passwords and Tiny Tapeout ports) 1 day.
- Single-flight: concurrent requests for the same URL share one fetch. Stale data is served
  (marked `stale: true`) if a refresh fails. Every result carries `as_of`.
- In-use checks and anything before a power-cycle always fetch live, never from cache.

### In-use check

The broker runs the stage 1 in-use script (shipped as package data, kept identical to the
one in `in-use.md` by a test) over the master, excluding the broker's own connection, and
the Tiny Tapeout rule against `status.json`. Verdicts: `idle`, `in use`, `unknown`, plus
the local claim. Callers treat `unknown` as `in use`. Since 2026-11-27 (2026-11-27-tt-boards-over-ssh-design.md) a Tiny
Tapeout board gets both: the script on its welland Pi and the site rule, combined (in use if
either says so, else unknown if either does, else idle); a welland Pi that carries a Tiny
Tapeout board gets that board's site rule as well. Since 2026-11-27
(2026-11-27-tmux-idle-design.md) tmux key presses count for 5 minutes (300 s): a tmux client
with a key press under 300 s is in use, and a program in a tmux window is in use only while
its session (`#{session_activity}`, which key presses move and output does not) had a key
press under 300 s; otherwise it is a signal on the verdict, naming its pid, the program and the
minutes since the last key press. ssh terminals and `~/Uploads` stay at 15 minutes (900 s),
and a command running for another login is always in use.

**The claim holder's own activity.** For the holder of a live claim, two signals are
discounted because they are the holder's own doing: `~/Uploads` changes newer than the
claim's start, and on Tiny Tapeout an `idle_display.state` of `waiting` or `left: …` that
began after the claim's start (the broker records `status.json` at claim time to tell).
Every other signal — another login, a tmux client, another login's running command, a
non-zero `clients` count — still refuses. So "load a design, it hangs, power-cycle" works
for the holder, while a human who arrives during the claim still stops it.

### Packaging

- Python ≥3.12, `uv`. MCP front end on the official `mcp` SDK or FastMCP — the builder
  picks after checking current versions — pinned exactly. httpx for site calls.
- Versions come from `git describe` (`build_version.py`): `v<X>.<Y>-<N>-g<hash>` becomes
  `<X>.<Y>.<N>`, so every commit on main has its own, increasing number. No release tags
  beyond the `v0.0` base tag on the first commit, no hand-edited version.
- CI publishes every push to main to PyPI as `fpgas-online-mcp` (trusted publishing, the
  `pypi` environment), after the tests pass.
- The plugin manifest's `mcpServers` (`.claude-plugin/plugin.json`, not a root `.mcp.json`,
  which would also act as a project config for contributors) runs `uvx
  fpgas-online-mcp@latest`, so installing the plugin installs the server. It cannot build
  from the plugin directory: Claude Code installs a copy without `.git` (checked
  2026-11-26), so `git describe` has nothing to read there. The manifest has no `version`,
  so Claude Code uses the commit SHA and every push is an update.
- For cloud sessions (or any repo), the README gives the same `uvx fpgas-online-mcp@latest`
  as a repo-level `.mcp.json` entry.
- Sessions on big-storage are not sandboxed (checked 2026-11-25), so their shells can use
  the broker socket and the ssh ControlPath sockets. The README notes that a sandboxed
  session must allow `$XDG_RUNTIME_DIR/fpgas-online/` for the CLI and shared ssh to work.
- The Tiny Tapeout sessions' hand-made claims directory
  (`~/github/TinyTapeout/tmp/board-claims/`) holds no claims today (only a lock file), so
  the server does not read it; once the server ships, those sessions are told to use it. The stage 1 skill stays; its SKILL.md gains one line pointing to the
  tools when present, and to the claims directory for skill-only agents.
- README: install, the CLI, how to stop the broker (`fpgas-online broker stop`), where state
  lives.

### Testing

- Unit tests with a fake site (httpx mock transport) and a fake `ssh` executable on `PATH`:
  cache TTL/single-flight/stale, claims (expiry, renew, conflicts, override, disconnected
  holder), broker start-up race (two front ends at once start one broker), version restart
  refusal with claims held, power-cycle wait (boot id unchanged → fail loudly; empty values
  → not back), not-PoE-powered report, untrusted-output wrapping and truncation.
- MCP-level tests through an in-memory client: tool list, annotations, error texts.
- A manual live smoke test script (read-only by default; power-cycle only with a flag and a
  named board), run before merge against welland `pi-sw1-p2` and ps1 `pi21`.

## Left to the builder

- `mcp` SDK versus FastMCP, and exact pins.
- How the front end learns a session name, if the environment offers one.
- File formats for cache and claim files (JSON, documented in the README).
- Internal module layout.
