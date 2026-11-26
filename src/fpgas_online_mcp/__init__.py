"""Shared broker, MCP server and CLI for the boards on fpgas.online."""

from importlib.metadata import PackageNotFoundError, version

# Set at build time from `git describe` (build_version.py), e.g. 0.0.80.
try:
    __version__ = version("fpgas-online-mcp")
except PackageNotFoundError:  # imported from src without being installed
    __version__ = "0.0.0+unknown"
