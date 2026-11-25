"""`fpgas-online`: the broker's requests from a shell (for humans and agents without MCP)."""

import argparse
import asyncio
import getpass
import json
import socket
import sys
from datetime import UTC, datetime
from typing import Any

from . import __version__
from .client import BrokerClient, BrokerError, connect

__all__ = ["main", "run"]


def holder() -> str:
    return f"cli:{getpass.getuser()}@{socket.gethostname()}"


def hhmm(iso: str | None) -> str:
    """'2026-11-26T12:05:09Z' -> '12:05' (UTC); anything unparseable is shown as is."""
    if not iso:
        return "-"
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return t.astimezone(UTC).strftime("%H:%M")


def when(iso: str) -> str:
    """'HH:MM UTC', with the ISO date added when it is not today (UTC)."""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return iso
    day = "" if t.date() == datetime.now(UTC).date() else f"{t.date().isoformat()} "
    return f"{day}{t:%H:%M} UTC"


def table(headers: list[str], rows: list[list[str]]) -> str:
    cells = [headers, *rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells)


def _presence(c: dict) -> str:
    if c.get("holder_connected"):
        return ""
    return " (cli)" if str(c.get("holder", "")).startswith("cli:") else " (not connected)"


def _claim_text(c: dict) -> str:
    return f"{c.get('holder')} until {hhmm(c.get('expires'))} UTC ({c.get('purpose')})"


# --- formatters (result of the op -> text) --------------------------------------------------------


def fmt_boards(r: dict) -> str:
    rows = []
    for b in r["boards"]:
        claim = b.get("claim")
        rows.append(
            [
                b["id"],
                b.get("type") or "-",
                b.get("condition") or "-",
                "yes" if b.get("offered") else "no" if b.get("offered") is not None else "-",
                hhmm(b.get("last_seen")),
                _claim_text(claim) if claim else "-",
            ]
        )
    out = [table(["BOARD", "TYPE", "CONDITION", "OFFERED", "SEEN", "CLAIM"], rows)]
    for site, info in sorted(r.get("sites", {}).items()):
        stale = " (stale)" if info.get("stale") else ""
        as_of = info.get("as_of")
        out.append(
            f"{site}: {info.get('note')}; {info.get('boards')} boards" + (f"; as of {as_of}" if as_of else "") + stale
        )
    if r.get("more"):
        out.append(r["more"])
    return "\n".join(out)


def fmt_status(r: dict) -> str:
    b = r["board"]
    lines = [f"{b['id']}: {b.get('type') or '-'}, {b.get('condition') or '-'}"]
    for key in ("boot_id", "usb_serial", "last_seen", "as_of"):
        if b.get(key):
            lines.append(f"  {key}: {b[key]}")
    if b.get("stale"):
        lines.append("  (site data is stale)")
    if b.get("claim"):
        lines.append(f"  claim {b['claim']['claim_id']}: {_claim_text(b['claim'])}")
    v = r.get("in_use")
    if v:
        lines.append(f"in use: {v['verdict']} (as of {v['as_of']})")
        for s in v.get("signals", []):
            lines.append(f"  signals: {s}")
        if v.get("note"):
            lines.append(f"  {v['note']}")
    return "\n".join(lines)


def fmt_claims(r: dict) -> str:
    if not r["claims"]:
        return "no live claims"
    rows = [
        [
            c["board"],
            c["claim_id"],
            c["holder"] + _presence(c),
            f"{hhmm(c['expires'])} UTC",
            c["purpose"],
        ]
        for c in r["claims"]
    ]
    return table(["BOARD", "CLAIM", "HOLDER", "EXPIRES", "PURPOSE"], rows)


def fmt_claim(r: dict) -> str:
    lines = [
        f"CLAIM ID: {r['claim_id']}",
        f"{r['board']} is yours until {when(r['expires'])} (started {when(r['started'])}); purpose: {r['purpose']}",
    ]
    if r.get("override_reason"):
        lines.append(f"override: {r['override_reason']}")
    if r.get("in_use"):
        lines.append(f"in-use check: {r['in_use']['verdict']}")
    lines.append(f"release with: fpgas-online release {r['claim_id']}")
    return "\n".join(lines)


def fmt_release(r: dict) -> str:
    return f"released {r['released']}"


# --- commands -------------------------------------------------------------------------------------


async def _broker_op(args: argparse.Namespace) -> tuple[Any, Any]:
    """(result, formatter) for the board commands; connects (and starts the broker) as needed."""
    c = await connect(holder())
    try:
        match args.cmd:
            case "boards":
                kw = {"site": args.site} if args.site else {}
                return await c.call("list_boards", **kw), fmt_boards
            case "status":
                return await c.call("board_status", board=args.board), fmt_status
            case "claims":
                return await c.call("claims"), fmt_claims
            case "claim":
                kw = {"board": args.board, "purpose": args.purpose}
                if args.minutes is not None:
                    kw["minutes"] = args.minutes
                if args.override_reason:
                    kw["override_reason"] = args.override_reason
                return await c.call("claim_board", **kw), fmt_claim
            case "release":
                return await c.call("release_board", claim_id=args.claim_id), fmt_release
    finally:
        await c.close()
    raise AssertionError(args.cmd)


async def _broker_cmd(args: argparse.Namespace) -> tuple[Any, str | None, int]:
    """(result, text, exit code) for `broker status|stop`, which never start a broker."""
    try:
        c: BrokerClient = await connect(holder(), spawn=False)
    except BrokerError as e:
        if e.code == "not_running":
            return {"running": False}, "not running", 1 if args.action == "status" else 0
        raise
    try:
        if args.action == "status":
            info = await c.call("ping")
            text = f"running: version {info['version']}, pid {info['pid']}, started {info['started']}"
            if info["version"] != __version__:
                text += (
                    f"\nthis CLI is version {__version__}: the next board command replaces the broker once it is idle"
                )
            return info, text, 0
        try:
            result = await c.call("shutdown_if_idle")
        except BrokerError as e:
            if e.code != "busy":
                raise
            claims = (await c.call("claims"))["claims"]
            held = sorted({c["holder"] for c in claims})
            text = f"not stopped: the broker is busy; claim holders: {', '.join(held)}\n" + fmt_claims(
                {"claims": claims}
            )
            return {"stopping": False, "claims": claims}, text, 1
        return result, "stopped", 0
    finally:
        await c.close()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fpgas-online", description="Use the shared fpgas.online boards.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help)
        sp.add_argument("--json", action="store_true", help="print the broker's raw result as JSON")
        return sp

    add("boards", "list boards").add_argument("--site", help="only this site")
    add("status", "one board's details and in-use verdict").add_argument("board", help="e.g. welland:pi-sw1-p2")
    add("claims", "list live claims")
    sp = add("claim", "claim a board")
    sp.add_argument("board")
    sp.add_argument("--minutes", type=int, help="1-240 (the broker defaults to 30)")
    sp.add_argument("--purpose", required=True)
    sp.add_argument("--override-reason", help="claim although the in-use check says busy (ask the user first)")
    add("release", "release a claim").add_argument("claim_id")
    b = add("broker", "inspect or stop the broker")
    b.add_argument("action", choices=["status", "stop"])
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "broker":
            result, text, code = asyncio.run(_broker_cmd(args))
        else:
            result, fmt = asyncio.run(_broker_op(args))
            text, code = fmt(result), 0
    except (BrokerError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2) if args.json else text)
    return code


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()
