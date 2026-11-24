# fpgas.online-mcp

Tools that let an AI agent use the FPGA and Tiny Tapeout boards on
[fpgas.online](https://fpgas.online): a skill with guidance and command
examples, and an MCP server.

This is for people and agents who *use* the boards. It is not for developing
or administering the fpgas.online infrastructure.

## Status

Nothing here works yet. The work is planned in three stages:

1. A skill with a summary of what fpgas.online offers, links to the full
   documentation at <https://docs.fpgas.online>, and command examples that
   work with nothing more than `ssh` and `curl`.
2. A local MCP server that several agent sessions on one machine can share,
   with persistent ssh connections, power-cycling, board lists, an "in use"
   check and a local cache.
3. Possibly a hosted MCP endpoint, for clients that cannot run a local server.

## License

Apache 2.0, see [LICENSE](LICENSE).
