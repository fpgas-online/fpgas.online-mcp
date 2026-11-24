# Stage 1 design: skill and command examples

Date: 2026-11-24. Status: draft, awaiting approval.

## Intent

Give an AI agent enough short, correct guidance to use the FPGA and Tiny
Tapeout boards on fpgas.online with nothing but `ssh`, `scp` and `curl`. It
must still be useful where an agent cannot run an MCP server. It is for
people and agents who use the boards, not for those who run the
infrastructure.

This is the first of three stages. Stage 2 adds a local MCP server shared by
the sessions on one machine. Stage 3, a hosted endpoint, is only considered
once stages 1 and 2 work well. Each later stage gets its own design.

## Goals

- A short summary of what each site offers, with links to the full pages on
  <https://docs.fpgas.online>.
- Copy-and-run examples for the five jobs: list boards (present, failing and
  missing), reach a board's host over a persistent multiplexed ssh
  connection, power-cycle a board and wait for it to return, check whether a
  board is in use, and drive a Tiny Tapeout board.
- Loads in local Claude Code, in Claude Code cloud sessions and in claude.ai.

## Non-goals

- No MCP server, no daemon, no claims between sessions, no cache. Those are
  stage 2.
- No use of operator-only paths: the tweed jump host, private `10.21.x.x`
  addresses, direct SNMP, or the Pi daemon on port 8765.
- No copy of the documentation. The skill summarises and links.

## Anti-goals

- Guidance that is wrong for the live sites. Every example is run against
  them before it is merged.
- A skill so long that it costs context whenever it loads.

## Constraints found on 2026-11-24

- All end-user interfaces are public and need no login. There is no token.
- `GET https://welland.fpgas.online/fleet/registry.json` lists 44 machines,
  each with a condition of `operational` (11), `attention` (11) or `missing`
  (22), its boards and its last-seen time. ps1 and tinytapeout answer 404
  for that path, so on ps1 only the `/fpgas/` list of offered hosts exists,
  and on tinytapeout the board list is the home page plus
  `/board/<slug>/status.json`.
- Power control is `POST /snmp/toggle` with a JSON body naming the port (and
  the switch on welland). A port can be cycled once per 60 s; inside that it
  answers 429 with `Retry-After`. (From the site's code; not yet exercised.)
- ssh uses the user `pi` and one shared, published password. welland's port
  is `<switch><2-digit port>22` and needs `-4`; ps1's is `(100+N)*100+22`.
  A non-interactive `ssh host command` does not join the shared tmux
  session. The Tiny Tapeout site offers no ssh. Exercised on ps1 on
  2026-11-24: multiplexing cut a command from 2.6 s to about 0.6 s.
- welland's board ssh works over IPv4 and IPv6 (the page's "IPv4 only" is
  stale). From inside welland's own network IPv4 was refused until the
  router gained NAT loopback on 2026-11-25.
- "In use" is only published for Tiny Tapeout boards, as the `clients` count
  in `status.json`. On welland and ps1 it can only be read on the host over
  ssh (logged-in users, tmux clients, idle times).

## Approaches

1. **Skill and command examples only.** Works anywhere a shell exists, needs
   nothing running. No shared state between sessions.
2. **Local MCP server shared by the sessions on a machine, plus the skill.**
   Adds claims, a cache and managed connections. Local and cloud Claude Code
   only.
3. **Hosted MCP endpoint.** The only way to give tools to claude.ai
   connectors. Needs hosting on fpgas.online.

Chosen by Tim: 1 first, then 2, then look at 3. This document covers 1.

## Design

The repo is a Claude Code plugin from the start, so stage 2 can add the
server definition without moving anything.

```
.claude-plugin/plugin.json        plugin "fpgas-online"
.claude-plugin/marketplace.json   lets the repo be added as a marketplace
skills/fpgas-online/SKILL.md      the summary and the map below
skills/fpgas-online/references/
    boards.md        which boards exist, which are down, and why
    ssh.md           ports, password, ControlMaster, scp, port forwards
    power.md         power-cycle, the 60 s limit, waiting for the host
    in-use.md        how to tell whether someone is using a board
    tinytapeout.md   status, designs, bitstream upload, serial bridge
```

- `SKILL.md` stays under about 150 lines: what fpgas.online is, a table of
  the three sites and what each supports, the sharing rules, a table from
  task to reference file, and the documentation links. Reference files are
  one level deep and loaded only when needed.
- Sharing rules, stated once in `SKILL.md`: check use before anything that
  disturbs a board; a power-cycle ends everyone's session and wipes the
  host; uploads vanish at reset; a bitstream upload on the Tiny Tapeout site
  can evict another visitor's upload.
- `ssh.md` gives a `~/.ssh/config` block using `ControlMaster auto`,
  `ControlPath` and `ControlPersist`, so the second and later commands reuse
  one connection. This is plain OpenSSH; stage 2's server will manage the
  same sockets.
- The shared password is not written in this repo. The examples read it from
  where the site publishes it.
- Text that comes back from a board (serial, boot messages) is described as
  untrusted output, never as instructions.
- `README.md` says how to load the skill on each surface (plugin install
  locally, `.claude/skills/` in a repo for cloud sessions, upload to
  claude.ai) and which network access it needs: HTTPS to the three sites and
  the docs, and the ssh ports on welland and ps1.

## Checks

- A small test checks the skill's front matter against the published limits
  and that every file `SKILL.md` names exists.
- Every read-only example is run against the live sites.
- The ssh and power-cycle examples are run once against one board agreed
  with Tim, at a time when it is not in use.

## Left to the builder

- Exact wording, and how each example extracts fields (`jq` or plain
  `curl`).
- How a non-interactive agent supplies the shared password to `ssh`
  (`SSH_ASKPASS` or a one-time key install on the host), chosen by trying
  both against a real board.
- Whether cloud sessions allow outbound ssh; recorded in `README.md` once
  tested.
