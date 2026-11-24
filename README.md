# fpgas.online-mcp

Tools that let an AI agent use the FPGA and Tiny Tapeout boards on
[fpgas.online](https://fpgas.online): a skill with guidance and command
examples, and later an MCP server.

This is for people and agents who *use* the boards. It is not for developing
or administering the fpgas.online infrastructure.

## Status

Stage 1, the skill, is written. It needs only `curl`, `jq` and `ssh`. Plugin
name: `fpgas-online`; skill: `skills/fpgas-online/` (`SKILL.md` plus five
reference files).

## Install

Claude Code, local, as a plugin:

```
/plugin marketplace add fpgas-online/fpgas.online-mcp
/plugin install fpgas-online@fpgas-online
```

Claude Code cloud sessions: copy the folder `skills/fpgas-online` into your
repository's `.claude/skills/` (so it is `.claude/skills/fpgas-online/SKILL.md`),
or enable the skill on claude.ai, which cloud sessions load.

claude.ai: zip the `skills/fpgas-online` folder and upload the zip under
Customize > Skills. Code execution must be enabled; the skill's commands run
there.

## Network access

The skill talks to the sites over the network. In Claude Code cloud sessions
the environment has a network allowlist that you configure; allow:

- HTTPS (443) to `welland.fpgas.online`, `ps1.fpgas.online`,
  `tinytapeout.fpgas.online` and `docs.fpgas.online`.
- Outbound TCP for ssh to `welland.fpgas.online` on ports 10122-14022 and
  20122-24822 (pattern `<switch><2-digit port>22`, for example `10222`), and to
  `ps1.fpgas.online` on ports `(100+N)*100+22` (for example `10322` for `pi3`).
- WSS (443) to `tinytapeout.fpgas.online` for the serial bridge.

Whether a cloud session permits outbound ssh on those ports has not been tested
yet.

The shared ssh password is published on each board page, so it is not a secret
to protect, but do not commit it to a repository.

## Roadmap

1. The skill (this stage): a summary of what fpgas.online offers, links to the
   full documentation at <https://docs.fpgas.online>, and command examples that
   work with nothing more than `ssh` and `curl`.
2. A local MCP server that several agent sessions on one machine can share,
   with persistent ssh connections, power-cycling, board lists, an "in use"
   check and a local cache.
3. Possibly a hosted MCP endpoint, for clients that cannot run a local server.

## License

Apache 2.0, see [LICENSE](LICENSE).
