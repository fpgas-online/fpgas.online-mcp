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
from .client import BrokerClient, BrokerError, connect, ignored_option, version_tuple
from .fleet import SECTIONS
from .protocol import PROTOCOL
from .ssh import clean_text

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


def when_epoch(ts: Any) -> str:
    """A Unix timestamp (the broker's `started`) as `when` shows it; anything else as is."""
    try:
        return when(datetime.fromtimestamp(float(ts), UTC).isoformat())
    except (TypeError, ValueError, OverflowError, OSError):
        return str(ts)


def table(headers: list[str], rows: list[list[str]]) -> str:
    cells = [headers, *rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells)


def _presence(c: dict) -> str:
    if c.get("holder_connected"):
        return ""
    return " (cli)" if str(c.get("holder", "")).startswith("cli:") else " (not connected)"


def _claim_text(c: dict) -> str:
    unoffered = " (allow_unoffered)" if c.get("allow_unoffered") else ""
    return f"{c.get('holder')} until {hhmm(c.get('expires'))} UTC ({c.get('purpose')}){unoffered}"


def _port_text(p: dict) -> str:
    link = {True: "link up", False: "link down"}.get(p.get("link_up"), "link unknown")
    watts = f", {p['watts']:g} W" if isinstance(p.get("watts"), int | float) else ""
    return f"{link}, PoE {p.get('poe_state') or 'unknown'}{watts}"


# --- formatters (result of the op -> text) --------------------------------------------------------


def fmt_boards(r: dict) -> str:
    if "columns" not in r:
        raise BrokerError(
            "version", "the broker answered list_boards in an older format; run `fpgas-online broker stop`"
        )
    rows = []
    claims = r.get("claims", {})
    for row in r["boards"]:
        b = dict(zip(r["columns"], row, strict=True))
        claim = claims.get(b["id"])
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
    if b.get("boot_check"):
        lines.append(f"  boot check: {b['boot_check'].get('result') or '-'} at {b['boot_check'].get('at') or '-'}")
    for board in b.get("boards", []):
        tests = ", ".join(f"{k} {v}" for k, v in (board.get("tests") or {}).items())
        lines.append(
            f"  board {board.get('type') or '-'}: {board.get('result') or '-'}" + (f" ({tests})" if tests else "")
        )
    if b.get("cut"):
        lines.append(f"  ({b['cut']})")
    if b.get("switch_port"):
        lines.append(f"  switch port: {_port_text(b['switch_port'])}")
    if b.get("fleet_page"):
        lines.append(f"  fleet page: {b['fleet_page']}")
    if b.get("claim"):
        lines.append(f"  claim {b['claim']['claim_id']}: {_claim_text(b['claim'])}")
    for key in ("offered_note", "not_offered", "warning"):
        if r.get(key):
            lines.append(f"note: {r[key]}")
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
            c["purpose"] + (" (allow_unoffered)" if c.get("allow_unoffered") else ""),
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
    if r.get("allow_unoffered"):
        lines.append(
            "allow_unoffered: connect, run and power_cycle may reach this Pi although the site does not offer it"
        )
    for key in ("offered_note", "warning"):
        if r.get(key):
            lines.append(f"note: {r[key]}")
    if r.get("in_use"):
        lines.append(f"in-use check: {r['in_use']['verdict']}")
    lines.append(f"release with: fpgas-online release {r['claim_id']}")
    return "\n".join(lines)


def fmt_release(r: dict) -> str:
    return f"released {r['released']}"


_FLEET_HEAD = ("board", "host", "serial", "page", "as_of", "stale", "paging", "cut", "unavailable", "note")


def _compact(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def fmt_fleet(r: dict) -> str:
    on = f" on {r['host']}" if r.get("host") else ""
    stale = " (stale)" if r.get("stale") else ""
    lines = [f"{r.get('board')}{on} (serial {r.get('serial')}, as of {r.get('as_of') or '-'}){stale}"]
    lines.append(f"page: {r.get('page')}")
    paging = r.get("paging") or {}
    for name, value in r.items():
        if name in _FLEET_HEAD:
            continue
        title = name
        if name in paging:
            page = paging[name]
            nxt = f"; next: --offset {page['next']}" if "next" in page else ""
            title = f"{name} ({page['shown']} of {page['total']}{nxt})"
        if isinstance(value, dict):
            lines.append(f"{title}:")
            lines += [f"  {k}: {_compact(v)}" for k, v in value.items()]
        elif isinstance(value, list):
            lines.append(f"{title}:")
            lines += [f"  {_compact(v)}" for v in value]
        elif isinstance(value, str) and "\n" in value:  # wrapped board text: one item per line
            lines += [f"{title}:", f"  {value}"]
        else:
            lines.append(f"{title}: {_compact(value)}" if value is not None else f"{title}: -")
    for name, note in (r.get("cut") or {}).items():
        lines.append(f"cut {name}: {note}")
    if r.get("unavailable"):
        lines.append(f"not available: {', '.join(r['unavailable'])}")
    if r.get("note"):
        lines.append(f"note: {r['note']}")
    return clean_text("\n".join(lines))  # a Pi's strings may carry terminal escapes


def _cell(v: Any) -> str:
    """A table cell: any value as text, '-' for none."""
    return "-" if v is None or v == "" else str(v)


def fmt_switches(r: dict) -> str:
    lines = []
    if r.get("board"):
        on = f" on {r['host']}" if r.get("host") else ""
        lines.append(f"the port of {r['board']}{on}")
    for s in r.get("switches", []):
        lines.append(
            f"switch {_cell(s.get('index'))}: {_cell(s.get('name'))} ({_cell(s.get('model'))}), "
            f"{_cell(s.get('status'))}, read {_cell(s.get('read_at'))}; {_cell(s.get('empty_ports'))} empty ports"
        )
        issues = s.get("known_issues") or []
        if isinstance(issues, int):  # shortened to a count to fit
            lines.append(f"  {issues} known issues (switch_ports with this switch shows them)")
            issues = []
        for issue in issues:
            if not isinstance(issue, dict):
                lines.append(f"  known issue: {issue}")
                continue
            if issue.get("text"):
                where = ", ".join(str(issue[k]) for k in ("issue", "url") if issue.get(k))
                text = f"{issue['text']} ({where})"
            else:  # shortened to its issue and link to fit
                text = f"{_cell(issue.get('issue'))} ({_cell(issue.get('url'))})"
            lines.append(f"  known issue: {text}")
    rows, why = [], []
    for row in r.get("ports", []):
        p = dict(zip(r["columns"], row, strict=True))
        rows.append(
            [
                _cell(p.get("switch")),
                _cell(p.get("port")),
                ",".join(_cell(m) for m in p.get("machines") or []) or "-",
                "up" if p.get("link_up") else "down",
                _cell(p.get("speed")),
                _cell(p.get("poe_state")),
                _cell(p.get("watts")),
                _cell(p.get("vlan")),
                " ".join(_cell(x) for x in (p.get("lldp_name"), p.get("lldp_port")) if x not in (None, "")) or "-",
                f"{_cell(p.get('rx_errors'))}/{_cell(p.get('tx_errors'))}",
                "ok" if p.get("net_ok") else "NOT OK",
            ]
        )
        if p.get("net_ok") is False:
            why.append(f"{p.get('switch')}/{p.get('port')}: {p.get('net_title')}")
    headers = ["SWITCH", "PORT", "MACHINES", "LINK", "SPEED", "POE", "WATTS", "VLAN", "LLDP", "ERRORS", "NET"]
    lines.append(table(headers, rows))
    lines += why
    lines.append(f"{len(rows)} of {r.get('total')} ports, from offset {r.get('offset')}; as of {r.get('as_of')}")
    if r.get("more"):
        lines.append(r["more"])
    return clean_text("\n".join(lines))  # a Pi's strings may carry terminal escapes


# --- commands -------------------------------------------------------------------------------------


async def _call(c: BrokerClient, op: str, **kw: Any) -> Any:
    """c.call, saying what to do when the running broker is older than this CLI and lacks the op."""
    try:
        return await c.call(op, **kw)
    except BrokerError as e:
        if e.code != "unknown_op":
            raise
        raise BrokerError(
            "version",
            f"the running broker is older than this CLI and has no {op}; it is replaced once it is idle: run "
            "`fpgas-online broker stop` when no claims are held, or wait for the other sessions to end",
        ) from e


def _warn(op: str, kw: dict, result: Any) -> Any:
    """Add a warning when an older broker ignored allow_unoffered (see client.ignored_option)."""
    warning = ignored_option(op, kw, result)
    return {**result, "warning": warning} if warning else result


async def _broker_op(args: argparse.Namespace) -> tuple[Any, Any]:
    """(result, formatter) for the board commands; connects (and starts the broker) as needed."""
    c = await connect(holder())
    try:
        match args.cmd:
            case "boards":
                kw = {k: v for k, v in (("site", args.site), ("condition", args.condition)) if v}
                return await c.call("list_boards", **kw), fmt_boards
            case "status":
                kw = {"board": args.board}
                if args.allow_unoffered:
                    kw["allow_unoffered"] = True
                return _warn("board_status", kw, await c.call("board_status", **kw)), fmt_status
            case "claims":
                return await c.call("claims"), fmt_claims
            case "claim":
                kw = {"board": args.board, "purpose": args.purpose}
                if args.minutes is not None:
                    kw["minutes"] = args.minutes
                if args.override_reason:
                    kw["override_reason"] = args.override_reason
                if args.allow_unoffered:
                    kw["allow_unoffered"] = True
                return _warn("claim_board", kw, await c.call("claim_board", **kw)), fmt_claim
            case "release":
                return await c.call("release_board", claim_id=args.claim_id), fmt_release
            case "fleet":
                kw = {"board": args.board, "offset": args.offset}
                if args.section:
                    kw["sections"] = args.section
                if args.limit is not None:
                    kw["limit"] = args.limit
                return await _call(c, "fleet_details", **kw), fmt_fleet
            case "switches":
                kw = {"problems": args.problems, "offset": args.offset}
                for key in ("switch", "port", "board", "limit"):
                    if getattr(args, key) is not None:
                        kw[key] = getattr(args, key)
                return await _call(c, "switch_ports", **kw), fmt_switches
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
            text = f"running: version {info['version']}, pid {info['pid']}, started {when_epoch(info['started'])}"
            if info.get("protocol", 1) != PROTOCOL:
                text += (
                    f"\nthis CLI speaks protocol {PROTOCOL}, the broker {info.get('protocol', 1)}: board commands fail"
                )
            elif version_tuple(__version__) > version_tuple(info["version"]):
                text += (
                    f"\nthis CLI is version {__version__}: the next board command replaces the broker once it is idle"
                )
            elif info["version"] != __version__:
                text += f"\nthis CLI is version {__version__}: it uses the running (newer) broker"
            return info, text, 0
        try:
            result = await c.call("shutdown_if_idle")
        except BrokerError as e:
            if e.code != "busy":
                raise
            claims = (await c.call("claims"))["claims"]
            text = f"not stopped: the broker is busy ({e.message})"
            if claims:
                held = sorted({c["holder"] for c in claims})
                text += f"; claim holders: {', '.join(held)}\n" + fmt_claims({"claims": claims})
            return {"stopping": False, "reason": e.message, "claims": claims}, text, 1
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

    sp = add("boards", "list boards")
    sp.add_argument("--site", help="only this site")
    sp.add_argument("--condition", help="only boards in this condition, e.g. missing, attention, 'not connected'")
    unoffered = (
        "also reach a welland Pi that is up but not offered (only when that is what you want: it may be under "
        "maintenance)"
    )
    sp = add("status", "one board's details and in-use verdict")
    sp.add_argument("board", help="e.g. welland:pi-sw1-p2")
    sp.add_argument("--allow-unoffered", action="store_true", help=unoffered)
    add("claims", "list live claims")
    sp = add("claim", "claim a board")
    sp.add_argument("board")
    sp.add_argument("--minutes", type=int, help="1-240 (the broker defaults to 30)")
    sp.add_argument("--purpose", required=True)
    sp.add_argument("--override-reason", help="claim although the in-use check says busy (ask the user first)")
    sp.add_argument("--allow-unoffered", action="store_true", help=unoffered + "; the claim keeps it")
    add("release", "release a claim").add_argument("claim_id")
    sp = add("fleet", "welland's fleet page of a board's Pi: why it is not offered, its checks, events, console")
    sp.add_argument("board", help="e.g. welland:pi-sw1-p2, or a Tiny Tapeout board (its Pi)")
    sp.add_argument(
        "--section",
        action="append",
        choices=[*SECTIONS, "all"],
        help="a section to show (repeat for more; default machine, boards, network, switch_port)",
    )
    sp.add_argument("--limit", type=int, help="items per list section (the broker defaults to 20)")
    sp.add_argument("--offset", type=int, default=0, help="skip this many items of each list section (newest first)")
    sp = add("switches", "welland's switch ports: link, PoE, VLAN, LLDP, errors (read-only)")
    sp.add_argument("--switch", help="only this switch (number or name)")
    sp.add_argument("--port", type=int, help="only this port number")
    sp.add_argument("--board", help="only the port of this board (a Tiny Tapeout board: its Pi's port)")
    sp.add_argument("--problems", action="store_true", help="only ports not as they should be, or with errors")
    sp.add_argument("--limit", type=int, help="at most this many ports")
    sp.add_argument("--offset", type=int, default=0, help="skip this many ports")
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
