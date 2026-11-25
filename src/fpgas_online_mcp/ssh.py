"""One OpenSSH ControlMaster per board, shared by every local session.

The broker never edits ~/.ssh/config: it writes its own ssh_config (one Host block per
board connected so far) and an askpass script in the config dir, and starts masters with
`ssh -F <config> -MNf <alias>`. Sessions reuse the master through the same config.
"""

import asyncio
import contextlib
import os
import re
import shlex
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .boards import BoardId

SITE_HOSTS = {"welland": "welland.fpgas.online", "ps1": "ps1.fpgas.online"}
START_TIMEOUT_S = 30.0
OP_TIMEOUT_S = 15.0
HEAD = TAIL = 4096
MAX_OUTPUT = HEAD + TAIL

ASKPASS = """#!/bin/sh
d=$(dirname "$0")
case "$1" in
  *ps1.fpgas.online*) cat "$d/ps1.pw" ;;
  *welland.fpgas.online*) cat "$d/welland.pw" ;;
esac
"""

_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]?")
_ESC = re.compile(r"\x1b[ -/]*[0-~]?")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class MasterInfo:
    alias: str
    config_path: str
    ssh_line: str
    scp_line: str
    include_line: str


@dataclass(frozen=True)
class RunResult:
    exit_status: int | None
    output: str
    truncated: bool
    timed_out: bool


def wrap_untrusted(text: str) -> str:
    """Wrap board text so it cannot close the wrapper (a literal '</board-output' is defused)."""
    text = text.replace("</board-output", "&lt;/board-output")
    return f'<board-output untrusted="true">{text}</board-output>'


def clean_output(data: bytes) -> tuple[str, bool]:
    """Decode, strip terminal escapes and control characters, truncate to head + tail."""
    text = data.decode("utf-8", errors="replace")
    for pattern in (_OSC, _CSI, _ESC, _CONTROL):
        text = pattern.sub("", text)
    if len(text) <= MAX_OUTPUT:
        return text, False
    omitted = len(text) - MAX_OUTPUT
    return f"{text[:HEAD]}\n[… {omitted} bytes omitted …]\n{text[-TAIL:]}", True


def _write_private(path: Path, content: str, mode: int) -> None:
    """Atomically write path with the given mode, unless it already has this content and mode."""
    try:
        if path.read_text() == content and (path.stat().st_mode & 0o777) == mode:
            return
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class SshManager:
    def __init__(self, sites, ssh_bin: str = "ssh", config_dir: Path | None = None, runtime_dir: Path | None = None):
        self._sites = sites
        self._ssh = ssh_bin
        self._config_dir = Path(config_dir) if config_dir else paths.config_dir()
        self._runtime_dir = Path(runtime_dir) if runtime_dir else paths.runtime_dir()
        self._locks: dict[str, asyncio.Lock] = {}
        self._blocks: dict[str, str] | None = None

    @property
    def config_path(self) -> Path:
        return self._config_dir / "ssh_config"

    def alias(self, board: BoardId) -> str:
        return f"fpgas-{board.site}-{board.name}"

    def _require_ssh(self, board: BoardId) -> None:
        if board.site not in SITE_HOSTS:
            raise ValueError(f"{board} has no ssh: Tiny Tapeout boards are reached through the site's web API")

    def _info(self, board: BoardId) -> MasterInfo:
        alias, cfg = self.alias(board), shlex.quote(str(self.config_path))
        return MasterInfo(
            alias=alias,
            config_path=str(self.config_path),
            ssh_line=f"ssh -F {cfg} {alias} '<command>'",
            scp_line=f"scp -F {cfg} <file> {alias}:",
            include_line=f"Include {self.config_path}",
        )

    # --- config files -------------------------------------------------------------

    def _load_blocks(self) -> dict[str, str]:
        if self._blocks is None:
            self._blocks = {}
            try:
                text = self.config_path.read_text()
            except OSError:
                text = ""
            for chunk in re.split(r"(?m)^(?=Host )", text):
                if chunk.startswith("Host "):
                    self._blocks[chunk.split(None, 2)[1]] = chunk if chunk.endswith("\n") else chunk + "\n"
        return self._blocks

    def _block(self, board: BoardId, port: int) -> str:
        return (
            f"Host {self.alias(board)}\n"
            f"    HostName {SITE_HOSTS[board.site]}\n"
            f"    Port {port}\n"
            "    User pi\n"
            "    ControlMaster auto\n"
            f"    ControlPath {self._runtime_dir}/cm-%C\n"
            "    ControlPersist 30m\n"
            "    StrictHostKeyChecking accept-new\n"
            f"    UserKnownHostsFile {self._config_dir}/known_hosts\n"
            "    BatchMode no\n"
            "    PreferredAuthentications password,keyboard-interactive\n"
            "    NumberOfPasswordPrompts 1\n"
            "    ServerAliveInterval 30\n"
            "    ClearAllForwardings yes\n"
            "    RequestTTY no\n"
        )

    # --- ssh invocations ----------------------------------------------------------

    def _base(self, board: BoardId) -> list[str]:
        return [self._ssh, "-F", str(self.config_path)]

    async def _exec(self, args: list[str], timeout: float, env: dict[str, str] | None = None) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        task = asyncio.ensure_future(proc.communicate())
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            self._kill(proc)
            out, _ = await task
            return -1, out.decode("utf-8", errors="replace") + f"\n(timed out after {timeout:g} s)"
        out, _ = task.result()
        return proc.returncode, out.decode("utf-8", errors="replace")

    @staticmethod
    def _kill(proc) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

    async def _control(self, board: BoardId, op: str) -> int:
        code, _ = await self._exec([*self._base(board), "-O", op, self.alias(board)], OP_TIMEOUT_S)
        return code

    async def check(self, board: BoardId) -> bool:
        if board.site not in SITE_HOSTS:
            return False
        return await self._control(board, "check") == 0

    async def exit(self, board: BoardId) -> None:
        """Close the master. A master that is not running is not an error."""
        if board.site not in SITE_HOSTS:
            return
        await self._control(board, "exit")

    async def ensure_master(self, board: BoardId) -> MasterInfo:
        self._require_ssh(board)
        async with self._locks.setdefault(str(board), asyncio.Lock()):
            if await self.check(board):
                return self._info(board)
            page = await self._sites.board_page(board.site, board.name)
            if not page.password or page.ssh_port is None:
                raise RuntimeError(f"the board page of {board} gave no ssh password or port")
            password = page.password
            _write_private(self._config_dir / f"{board.site}.pw", password, 0o600)
            _write_private(self._config_dir / "askpass", ASKPASS, 0o700)
            blocks = self._load_blocks()
            blocks[self.alias(board)] = self._block(board, page.ssh_port)
            _write_private(self.config_path, "\n".join(blocks.values()), 0o600)

            env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
            env["SSH_ASKPASS"] = str(self._config_dir / "askpass")
            env["SSH_ASKPASS_REQUIRE"] = "force"
            code, out = await self._exec(
                [*self._base(board), "-o", "ClearAllForwardings=yes", "-MNf", self.alias(board)],
                START_TIMEOUT_S,
                env,
            )
            if code != 0 or not await self.check(board):
                detail = out.replace(password, "***").strip()
                raise RuntimeError(f"could not open the ssh master for {board} (exit {code}): {detail}")
            return self._info(board)

    async def run(self, board: BoardId, command: str, timeout_s: float) -> RunResult:
        self._require_ssh(board)
        await self.ensure_master(board)
        args = [
            *self._base(board),
            "-o",
            "BatchMode=yes",
            "-o",
            "ClearAllForwardings=yes",
            self.alias(board),
            "--",
            command,
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        task = asyncio.ensure_future(proc.communicate())
        done, _ = await asyncio.wait({task}, timeout=timeout_s)
        timed_out = not done
        if timed_out:
            self._kill(proc)
        data, _ = await task
        await proc.wait()
        output, truncated = clean_output(data)
        return RunResult(None if timed_out else proc.returncode, output, truncated, timed_out)
