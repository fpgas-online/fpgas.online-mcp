# Stage 2 design: a local MCP server shared by the sessions on one machine

Date: 2026-11-25. Status: draft, awaiting approval.

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
  have a 60 s hold (429 + `Retry-After`); ps1 has no hold and its port mapping is unverified,
  so ps1 cycles need an explicit user confirmation.
- The in-use logic and its limits are in `skills/fpgas-online/references/in-use.md`; the
  Tiny Tapeout idle rule (`clients` 0 twice ≥15 s apart and `idle_display.state == loaded`)
  is there too.

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
- **Version**: the broker reports its package version. On mismatch the front end asks it to
  restart, which it does only when it holds no active claims; otherwise the call returns an
  error saying so. The broker exits after 30 minutes with no connected front ends and no
  claims (ssh masters keep their own `ControlPersist` lifetime).

### Board identity

A board is `<site>:<name>`: `welland:pi-sw1-p2`, `ps1:pi3`, `tinytapeout:fpga-4`. Tools
accept that id (and a bare name when unambiguous). Results also carry the facts that
survive port moves where the site gives them: USB serial, board type, FPGA part.

### Holders and claims

- A holder is one Claude session: `<label>@<host>`, where the label is the session's name
  if the environment exposes one, otherwise its working directory and front-end pid. The
  broker tracks which front ends are connected, so a claim whose holder has disconnected is
  shown as such.
- `claim(board, minutes, purpose)` returns a `claim_id`. Default 30 minutes, maximum 240,
  renewable by claiming again with the same id. Claims are advisory and local to the machine.
- Claims are also written as one JSON file per board in
  `$XDG_STATE_HOME/fpgas-online/claims/` (holder, purpose, expiry), so the CLI and
  skill-only agents see them, and they survive a broker restart.
- A claim is refused when another live claim exists, or when the board's in-use check says
  "in use"/"unknown" — unless the caller passes `override_reason`, which is recorded in the
  claim and shown to others.

### Tools (8)

| Tool | What it does | Annotations |
| --- | --- | --- |
| `guide(topic?)` | Summary of what fpgas.online offers and the sharing rules; with a topic (`boards`, `ssh`, `power`, `in-use`, `tinytapeout`), that reference's key points plus the docs links. Text comes from the skill files shipped in the package. | read-only |
| `list_boards(site?, board_type?, include_offline=true)` | Merged inventory from cache: id, type, condition, offered, last_seen, local claim (holder, expiry), and `as_of` per site. Says per site how complete the list is. | read-only |
| `board_status(board, check_use=true)` | One board in detail: inventory facts, local claim, and (if `check_use`) a live in-use check with its signals. | read-only |
| `claim_board(board, minutes=30, purpose, override_reason?)` | Take or renew a claim (above). | not read-only, not destructive |
| `release_board(claim_id)` | Release. | not read-only |
| `connect(board)` | Ensure an ssh master to the host (needs a claim held by the caller, or the board idle); returns the alias, the generated config path, and the exact `ssh -F … <alias> 'cmd'` / `scp -F …` lines to use from the shell. | not read-only, not destructive |
| `run(board, command, timeout_s=60)` | Run one non-interactive command over the master. Requires the caller's claim. Returns exit status and output truncated to 8 KB (head and tail), wrapped as untrusted board output. | not read-only, destructive=false, open-world |
| `power_cycle(board, confirm_ps1=false)` | Requires the caller's claim and a fresh in-use check (any signal other than the caller's own → refuse). Closes the master, records the boot id (or Tiny Tapeout `uptime_s`), toggles, honours `Retry-After`, waits until the boot id changes and ssh/status answers (cap 5 minutes), and reports timings. On ps1 refuses unless `confirm_ps1=true` and says why. | destructive, not idempotent |

Errors are returned as tool results that say what to do next (for example "claimed by
X until 15:40; ask the user or pick another board").

### ssh

- The broker writes `~/.config/fpgas-online/ssh_config` with one `Host fo-<site>-<host>`
  block per board it has connected to (HostName, Port, User `pi`, `ControlMaster auto`,
  `ControlPath <runtime dir>/cm-%C`, `ControlPersist 30m`, `StrictHostKeyChecking
  accept-new`, `UserKnownHostsFile ~/.config/fpgas-online/known_hosts`), and an askpass
  script reading the per-site password file (0600) — the stage 1 layout. It never edits
  `~/.ssh/config`; `connect` tells the user the one `Include` line to add if they want
  plain `ssh fo-…` to work.
- Masters are started with `ssh -F <config> -MNf <alias>`; status with `-O check`; closed
  with `-O exit` before a power-cycle.

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
the local claim. Callers treat `unknown` as `in use`.

### Packaging

- Python ≥3.12, `uv`. MCP front end on the official `mcp` SDK or FastMCP — the builder
  picks after checking current versions — pinned exactly. httpx for site calls.
- The plugin's `.mcp.json` runs the front end from the plugin directory
  (`uv run --project ${CLAUDE_PLUGIN_ROOT} fpgas-online-mcp`), so installing the plugin
  installs the server. The stage 1 skill stays; its SKILL.md gains one line pointing to the
  tools when present, and to the claims directory for skill-only agents.
- README: install, the CLI, how to stop the broker (`fpgas-online broker stop`), where state
  lives.

### Testing

- Unit tests with a fake site (httpx mock transport) and a fake `ssh` executable on `PATH`:
  cache TTL/single-flight/stale, claims (expiry, renew, conflicts, override, disconnected
  holder), broker start-up race (two front ends at once start one broker), version restart
  refusal with claims held, power-cycle wait (boot id unchanged → fail loudly; empty values
  → not back), ps1 refusal, untrusted-output wrapping and truncation.
- MCP-level tests through an in-memory client: tool list, annotations, error texts.
- A manual live smoke test script (read-only by default; power-cycle only with a flag and a
  named board), run before merge against welland `pi-sw1-p2` and ps1 `pi21`.

## Left to the builder

- `mcp` SDK versus FastMCP, and exact pins.
- How the front end learns a session name, if the environment offers one.
- File formats for cache and claim files (JSON, documented in the README).
- Internal module layout.
