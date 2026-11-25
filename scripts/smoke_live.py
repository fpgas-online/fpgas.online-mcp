"""Live smoke test against the real fpgas.online sites, through the real broker.

Read-only by default: inventory of every site, board_status with the in-use check on welland
pi-sw1-p2 and ps1 pi21, and, for each of those boards that is idle, claim -> connect -> run `hostname` ->
release. `--power BOARD` (exact board id, e.g. welland:pi-sw1-p2) additionally does
claim -> power_cycle -> release on that board; it changes the board, so run it only on purpose.

Uses the same path as the CLI: client.connect() starts the broker if needed. The holder is an
isolated "smoke:<pid>". Run: uv run python scripts/smoke_live.py [--power BOARD]
"""

import argparse
import asyncio
import json
import os
import sys
import time

from fpgas_online_mcp.client import BrokerClient, BrokerError, connect

DEFAULT_BOARDS = ["welland:pi-sw1-p2", "ps1:pi21"]


def show(text: str, limit: int = 600) -> None:
    for line in text.splitlines() or [""]:
        print("    " + line[:limit])


async def step(label: str, c: BrokerClient, op: str, **args):
    """Call one op, print the elapsed time and a short result; return (result, error)."""
    t = time.monotonic()
    try:
        result = await c.call(op, **args)
        err = None
    except BrokerError as e:
        result, err = None, e
    dt = time.monotonic() - t
    print(f"[{dt:7.2f}s] {label}: {'ERROR ' + err.code if err else 'ok'}")
    if err:
        show(err.message)
    return result, err


async def inventory(c: BrokerClient) -> None:
    r, _ = await step("list_boards (all sites)", c, "list_boards")
    if not r:
        return
    for site, info in sorted(r["sites"].items()):
        print(f"    {site}: {info.get('boards')} boards; {info.get('note')}; as_of {info.get('as_of')}")
    by_cond: dict[str, int] = {}
    for b in r["boards"]:
        by_cond[str(b.get("condition"))] = by_cond.get(str(b.get("condition")), 0) + 1
    print(f"    conditions in the (possibly cut) list: {by_cond}")
    if r.get("more"):
        print(f"    {r['more']}")


async def exercise(c: BrokerClient, board: str, power: bool) -> None:
    print(f"\n== {board} ==")
    r, err = await step("board_status (in-use check)", c, "board_status", board=board)
    if err or not r:
        print("    skipped: no status")
        return
    use = r.get("in_use") or {}
    print(f"    verdict: {use.get('verdict')}; signals: {use.get('signals')}")
    if use.get("verdict") != "idle":
        print(f"    skipped claim/connect/run: verdict is {use.get('verdict')}, not idle")
        return
    claim, err = await step("claim_board", c, "claim_board", board=board, purpose="smoke test", minutes=10)
    if err or not claim:
        return
    cid = claim["claim_id"]
    try:
        if power:
            r, _ = await step("power_cycle", c, "power_cycle", board=board, claim_id=cid)
            if r:
                print(f"    ok={r['ok']} seconds_to_back={r['seconds_to_back']} message={r['message']}")
                for s in r["steps"]:
                    print(f"      step: {s}")
        else:
            r, _ = await step("connect", c, "connect", board=board, claim_id=cid)
            if r:
                print(f"    alias {r['alias']}")
                print(f"    ssh: {r['ssh']}")
            r, _ = await step("run hostname", c, "run", board=board, claim_id=cid, command="hostname")
            if r:
                print(f"    exit_status={r['exit_status']} timed_out={r['timed_out']}")
                show(r["output"])
    finally:
        await step("release_board", c, "release_board", claim_id=cid)


async def main_async(power: str | None) -> int:
    t0 = time.monotonic()
    t = time.monotonic()
    c = await connect(f"smoke:{os.getpid()}")
    print(f"[{time.monotonic() - t:7.2f}s] connect to broker (started if needed), version {c.broker_version}")
    try:
        await inventory(c)
        if power:
            await exercise(c, power, True)
        else:
            for board in DEFAULT_BOARDS:
                await exercise(c, board, False)
        r, _ = await step("claims left over", c, "claims")
        if r:
            print("    " + json.dumps(r["claims"]))
    finally:
        await c.close()
    print(f"\ntotal {time.monotonic() - t0:.1f}s")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--power", metavar="BOARD", help="also power-cycle this board (exact id, e.g. welland:pi-sw1-p2)")
    args = p.parse_args()
    if args.power and ":" not in args.power:
        print("--power needs the exact board id '<site>:<name>'", file=sys.stderr)
        return 2
    return asyncio.run(main_async(args.power))


if __name__ == "__main__":
    raise SystemExit(main())
