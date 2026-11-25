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
GRACE_S = 5.0
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
_CLOSER = re.compile(r"</\s*board-output", re.IGNORECASE)


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


def defuse(text: str) -> str:
    """Neutralise any '</board-output' in board text."""
    return _CLOSER.sub("&lt;/board-output", text)


def wrap_untrusted(text: str) -> str:
    """Wrap board text so it cannot close the wrapper (any '</board-output' is defused)."""
    text = defuse(text)
    return f'<board-output untrusted="true">{text}</board-output>'


def _clean(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    for pattern in (_OSC, _CSI, _ESC, _CONTROL):
        text = pattern.sub("", text)
    return text


def clean_output(head: bytes, tail: bytes, total: int) -> tuple[str, bool]:
    """Clean head+tail (the first and last bytes of `total` bytes of output); mark what was omitted."""
    omitted = total - len(head) - len(tail)
    if omitted <= 0:
        return _clean(head + tail), False
    return f"{_clean(head)}\n[… {omitted} bytes omitted …]\n{_clean(tail)}", True


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
        for d in (self._config_dir, self._runtime_dir):
            if "%" in str(d) or '"' in str(d):
                raise ValueError(f'directory {d} contains "%" or a double quote, which ssh_config cannot take safely')
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
            include_line=f'Include "{self.config_path}"',
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
                    self._blocks[chunk.split(None, 2)[1]] = chunk.rstrip("\n") + "\n"
        return self._blocks

    def _block(self, board: BoardId, port: int) -> str:
        return (
            f"Host {self.alias(board)}\n"
            f"    HostName {SITE_HOSTS[board.site]}\n"
            f"    Port {port}\n"
            "    User pi\n"
            "    ControlMaster auto\n"
            f'    ControlPath "{self._runtime_dir}/cm-%C"\n'
            "    ControlPersist 30m\n"
            "    StrictHostKeyChecking accept-new\n"
            f'    UserKnownHostsFile "{self._config_dir}/known_hosts"\n'
            "    BatchMode no\n"
            "    PreferredAuthentications password,keyboard-interactive\n"
            "    NumberOfPasswordPrompts 1\n"
            "    ServerAliveInterval 30\n"
            "    ConnectTimeout 5\n"
            "    ClearAllForwardings yes\n"
            "    RequestTTY no\n"
        )

    # --- process helpers ----------------------------------------------------------

    @staticmethod
    def _kill(proc) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

    async def _supervise(self, args, timeout, *, env=None, stdout, reader=None):
        """Spawn in its own session, wait at most `timeout`, kill the group on timeout.

        Returns (returncode or None, timed_out, reader result). Every wait after the kill is bounded.
        """
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        task = asyncio.ensure_future(reader(proc.stdout)) if reader else None
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            timed_out = True
            self._kill(proc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), GRACE_S)
        result = None
        if task:
            try:
                result = await asyncio.wait_for(task, GRACE_S)
            except TimeoutError:
                pass
        return (None if timed_out else proc.returncode), timed_out, result

    async def _exec(self, args: list[str], timeout: float, env: dict[str, str] | None = None) -> tuple[int | None, str]:
        """Run a short ssh command, output to a private temp file (a daemonised master could hold a pipe open)."""
        fd, name = tempfile.mkstemp(dir=self._runtime_dir, prefix=".out-")
        try:
            try:
                code, timed_out, _ = await self._supervise(args, timeout, env=env, stdout=fd)
            finally:
                os.close(fd)
            out = Path(name).read_text(errors="replace")
        finally:
            with contextlib.suppress(OSError):
                os.unlink(name)
        if timed_out:
            out += f"\n(timed out after {timeout:g} s)"
        return (-1 if timed_out else code), out

    @staticmethod
    async def _read_bounded(stream) -> tuple[bytes, bytes, int]:
        """Read to EOF keeping only the first HEAD and last TAIL bytes; returns (head, tail, total)."""
        head, tail, total = bytearray(), bytearray(), 0
        while chunk := await stream.read(65536):
            total += len(chunk)
            room = HEAD - len(head)
            if room > 0:
                head += chunk[:room]
                chunk = chunk[room:]
            tail += chunk
            if len(tail) > 2 * TAIL:
                del tail[:-TAIL]
        del tail[:-TAIL]
        return bytes(head), bytes(tail), total

    def _base(self) -> list[str]:
        return [self._ssh, "-F", str(self.config_path)]

    async def _control(self, board: BoardId, op: str) -> int | None:
        code, _ = await self._exec([*self._base(), "-O", op, self.alias(board)], OP_TIMEOUT_S)
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
                [*self._base(), "-o", "ClearAllForwardings=yes", "-MNf", self.alias(board)],
                START_TIMEOUT_S,
                env,
            )
            if code != 0 or not await self.check(board):
                detail = out.replace(password, "***").strip()
                raise RuntimeError(f"could not open the ssh master for {board} (exit {code}): {detail}")
            return self._info(board)

    async def run(self, board: BoardId, command: str, timeout_s: float) -> RunResult:
        self._require_ssh(board)
        if not command.strip():
            raise ValueError("command is empty")
        await self.ensure_master(board)
        args = [
            *self._base(),
            "-o",
            "BatchMode=yes",
            "-o",
            "ClearAllForwardings=yes",
            self.alias(board),
            "--",
            command,
        ]
        code, timed_out, data = await self._supervise(
            args, timeout_s, stdout=subprocess.PIPE, reader=self._read_bounded
        )
        head, tail, total = data or (b"", b"", 0)
        output, truncated = clean_output(head, tail, total)
        return RunResult(code, output, truncated, timed_out)
