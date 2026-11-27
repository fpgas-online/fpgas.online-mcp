# fpgas.online-mcp

Tools that let an AI agent use the FPGA and Tiny Tapeout boards on
[fpgas.online](https://fpgas.online): a skill with guidance and command
examples, and a local MCP server (shared by all agent sessions on one machine)
with persistent ssh connections, claims, an "in use" check, power-cycling and a
cache.

This is for people and agents who *use* the boards. It is not for developing
or administering the fpgas.online infrastructure.

## Status

Stage 1 (the skill, `skills/fpgas-online/`) and stage 2 (the local MCP server
`fpgas-online-mcp`, a shared broker process and the `fpgas-online` CLI) are
written. A hosted endpoint (stage 3) is not started.

There are no releases to wait for: every push to `main` is published to
[PyPI](https://pypi.org/project/fpgas-online-mcp/) as `fpgas-online-mcp`, numbered
by `git describe` (`0.0.80` is the 80th commit after the `v0.0` tag on the first
commit), and everything below runs the latest one.

## Install

Claude Code, local, as a plugin (the plugin carries the skill and, through
`mcpServers` in `.claude-plugin/plugin.json`, the MCP server; it needs
[`uv`](https://docs.astral.sh/uv/) on `PATH`):

```
/plugin marketplace add fpgas-online/fpgas.online-mcp
/plugin install fpgas-online@fpgas-online
```

The plugin starts the MCP server with `uvx fpgas-online-mcp@latest`, so each
session runs the newest version on PyPI. The first launch downloads it and its
dependencies, which can take a while; if Claude Code reports that the server
timed out while starting, raise `MCP_TIMEOUT` (milliseconds, for example
`MCP_TIMEOUT=60000 claude`) or simply restart once the download is done.

Claude Code cloud sessions and other repositories: add the MCP server to the
repository's `.mcp.json` (also needs `uv`):

```json
{"mcpServers": {"fpgas-online": {"command": "uvx", "args": ["fpgas-online-mcp@latest"]}}}
```

`@latest` makes `uvx` check PyPI for a newer version at every start (without it
`uvx` keeps using the copy it cached first), so the server does not start when
PyPI cannot be reached, even with a cached copy. Without `uvx`:
`pip install -U fpgas-online-mcp` gives the `fpgas-online-mcp` and `fpgas-online`
commands. For the skill alone (no MCP), copy
`skills/fpgas-online` to `.claude/skills/fpgas-online/`.

claude.ai: zip the `skills/fpgas-online` folder and upload the zip under
Customize > Skills (skill only; code execution must be enabled). The claude.ai
code sandbox probably cannot open ssh connections to these hosts (not
verified), so expect only the HTTPS parts to work there.

## The MCP tools

| Tool | What it does |
| --- | --- |
| `guide` | The skill text: sites, sharing rules, topics |
| `list_boards` | All boards of all sites, one dense row each (condition, last seen) plus local claims; filter by site, condition or type |
| `board_status` | One board's facts (for a Tiny Tapeout board, the welland Pi it is plugged into) and the live in-use check (idle / in use / unknown) |
| `claim_board` | Claim a board (30 min default, 240 max); runs the in-use check first; renews with `claim_id`. A Tiny Tapeout board's claim holds the welland Pi it was on when claimed (no one can claim that Pi meanwhile); if the board moves, `connect`, `run` and `power_cycle` refuse until you claim it again |
| `release_board` | Release a claim |
| `connect` | Open the shared ssh connection (a Tiny Tapeout board's: to the welland Pi it is plugged into); returns the ssh/scp command lines |
| `run` | Run one non-interactive command on the host (output at most 8 KB, wrapped as untrusted) |
| `power_cycle` | Cycle the port and wait for a real reboot (about 8 minutes at worst) |
| `fleet_details` | welland's page of one Pi (a Tiny Tapeout board: its Pi): machine, boards, network and switch port by default; boot checks, labels, registrations, events and console on request; paged, at most 8 KB, the Pi's own text wrapped as untrusted. Read-only; not for ps1 |
| `switch_ports` | welland's switch ports (link, PoE state and watts, VLAN, LLDP, errors); filter by switch, port, board or problems. Read-only: it never switches a port; not for ps1 |

`connect`, `run` and `power_cycle` need your claim. Unknown in-use state counts
as in use.

Tool-call timeout: Claude Code's default for MCP tool calls is about 28 hours
(`MCP_TOOL_TIMEOUT`, milliseconds; a per-server `timeout` field in `.mcp.json`
overrides it), so the 5 to 8 minutes a `power_cycle` can take are fine. Other
clients may have shorter limits.

## Command line

`fpgas-online` talks to the same broker (it starts it if needed), for people
and for agents without MCP:

```
fpgas-online boards [--site SITE] [--condition CONDITION]
fpgas-online status BOARD
fpgas-online claims
fpgas-online claim BOARD --purpose TEXT [--minutes N] [--override-reason TEXT]
fpgas-online release CLAIM_ID
fpgas-online fleet BOARD [--section SECTION ...] [--limit N] [--offset N]
fpgas-online switches [--switch N] [--port N] [--board BOARD] [--problems] [--limit N] [--offset N]
fpgas-online broker status|stop
```

Add `--json` for the raw result. Run it with `uvx --from fpgas-online-mcp@latest fpgas-online ...`
or `uv run fpgas-online ...` in a checkout. `fpgas-online broker stop` stops
the broker unless claims are live (it says who holds them) or another session
is connected to it.

## Where state lives

Directories are created mode 0700 (secret files 0600), and follow the XDG
variables.

| What | Where |
| --- | --- |
| Broker socket and lock | `$XDG_RUNTIME_DIR/fpgas-online/` (fallback `$XDG_CACHE_HOME/fpgas-online/run/`) |
| Claims | `$XDG_STATE_HOME/fpgas-online/claims/<site>_<name>.json`; broker log `broker.log` beside it |
| Cache of site data | `$XDG_CACHE_HOME/fpgas-online/` |
| ssh config, askpass script, known_hosts, `<site>.pw` | `$XDG_CONFIG_HOME/fpgas-online/` |

The defaults are `~/.local/state`, `~/.cache` and `~/.config`. The broker never
edits `~/.ssh/config`.

Claims are advisory and local to this machine: they stop other sessions and the
CLI here from taking a board, not anyone on another machine or on the website.
A claim file looks like:

```json
{
  "claim_id": "c-AbC123xyZ9k",
  "board": "welland:pi-sw1-p2",
  "holder": "my-session@myhost",
  "purpose": "blink test",
  "started": "2026-11-26T12:00:00Z",
  "expires": "2026-11-26T12:30:00Z",
  "override_reason": null,
  "tt_state_at_claim": null
}
```

Agents without the MCP tools can see who holds what with `fpgas-online claims`
or by reading those files.

## Stopping the broker

One broker per user serves all sessions and exits when idle. Stop it with
`fpgas-online broker stop` (refused while claims are live or another session is
connected). A newer client replaces an older broker once it is idle; until then,
and for older clients, sessions of different versions share the running broker.

When upgrading, run one broker version per machine: stop the old broker (or let
it go idle) before using the new one. An older broker sharing the claims
directory does not apply the one-claim-per-Pi rule (a Tiny Tapeout board and the
welland Pi it is plugged into), so a claim taken through it can overlap one taken
through the new broker.

## Sandbox

If Claude Code's sandbox is on, allow reads and writes in
`$XDG_RUNTIME_DIR/fpgas-online/` (the broker socket and the ssh control
sockets), and the state, cache and config directories above, and allow the
network access below.

## Network access

The skill, the MCP server and its broker (and the `fpgas-online` CLI) all talk
to the sites over the network. In Claude Code cloud sessions the environment has
a network allowlist that you configure; allow:

- HTTPS (443) to `welland.fpgas.online`, `ps1.fpgas.online`,
  `tinytapeout.fpgas.online` and `docs.fpgas.online`.
- Outbound TCP for ssh to `welland.fpgas.online` on ports 10122-14022 and
  20122-24822 (pattern `<switch><2-digit port>22`, for example `10222`; the
  Tiny Tapeout boards' Pis are among them), and to
  `ps1.fpgas.online` on ports `(100+N)*100+22` (for example `10322` for `pi3`;
  the hosts listed on 2026-11-25, `pi2` to `pi23`, use 10222-12322).
- WSS (443) to `tinytapeout.fpgas.online` for the serial bridge.

Whether a cloud session permits outbound ssh on those ports has not been tested
yet.

The shared ssh password is published on each board page, so it is not a secret
to protect, but do not commit it to a repository.

## Roadmap

1. The skill (done).
2. A local MCP server that several agent sessions on one machine share (done:
   this version).
3. Possibly a hosted MCP endpoint, for clients that cannot run a local server.

## License

Apache 2.0, see [LICENSE](LICENSE).
