"""scripts/smoke_live.py guards (no network: the broker connection is faked)."""

import importlib.util
from pathlib import Path

import pytest

from fpgas_online_mcp.protocol import BrokerError

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smoke_live.py"


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("smoke_live", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("board", ["tinytapeout:fpga-1", "welland:pi-sw2-p13", "ps1:pi7"])
def test_power_refuses_protected_boards(smoke, board, capsys, monkeypatch):
    def no_connect(*a, **k):
        raise AssertionError("must not connect")

    monkeypatch.setattr(smoke, "connect", no_connect)
    assert smoke.main(["--power", board]) == 2
    assert "refuses" in capsys.readouterr().err


class FailingClient:
    broker_version = "x"

    async def call(self, op, **args):
        raise BrokerError("site_unavailable", "down")

    async def close(self):
        pass


def test_any_failed_step_exits_1(smoke, capsys, monkeypatch):
    async def fake_connect(holder):
        return FailingClient()

    monkeypatch.setattr(smoke, "connect", fake_connect)
    assert smoke.main([]) == 1
    assert "FAILED steps" in capsys.readouterr().out
