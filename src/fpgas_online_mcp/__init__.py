"""Shared broker, MCP server and CLI for the boards on fpgas.online."""

from importlib.metadata import version

# Set at build time from `git describe` (build_version.py), e.g. 0.0.80.
__version__ = version("fpgas-online-mcp")
