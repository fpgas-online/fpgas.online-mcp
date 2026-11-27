"""The in-use script itself, run locally under plain `sh` with fake `tmux`, `ps`, `who`, `ss` and `date`.

Each awk on the host may be mawk (Raspberry Pi OS's default), gawk or busybox, so every case runs with
each one installed here. No ssh: the script reads only what the fakes print.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from tmpdir import TEST_TMP

from fpgas_online_mcp import inuse

AWKS = [a for a in ("mawk", "gawk", "busybox") if shutil.which(a)]

# `ps -u ... -o pid=,ppid=,etimes=,args=`: the script finds its own login by walking up from `$$` to an
# `sshd-session: pi@...` line, so the fake prints the real chain of the script's processes up to the
# test process, which it calls the sshd of this login, then the extra lines of the case.
FAKE_PS = r"""#!/bin/sh
if [ "$1" = -A ]; then cat "$FAKE/ps_a"; exit 0; fi
p=$$
while [ -n "$p" ] && [ "$p" != 0 ]; do
  pp=$(awk '{ sub(/.*\) /, ""); print $2 }' "/proc/$p/stat")
  if [ "$p" = "$FAKE_SSHD" ]; then echo "$p 1 100 sshd-session: pi@notty"; break; fi
  echo "$p $pp 1 sh"
  p=$pp
done
cat "$FAKE/ps_u"
"""

FAKE_TMUX = r"""#!/bin/sh
case "$1" in
  list-clients) f=clients ;;
  list-panes) f=panes ;;
  *) echo "fake tmux: $*" >&2; exit 2 ;;
esac
if [ -f "$FAKE/$f.err" ]; then cat "$FAKE/$f.err" >&2; exit 1; fi
echo "$*" >> "$FAKE/tmux_args"
cat "$FAKE/$f"
"""


@pytest.fixture
def scratch():
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix="inuse-sh-", dir=TEST_TMP))
    yield d
    shutil.rmtree(d)


def run_script(
    scratch: Path,
    awk: str,
    *,
    clients: str | None = "",
    panes: str = "",
    ps_a: str = "",
    ps_u: str = "",
    who: str = "",
    uploads_age_s: int | None = None,
    now: int | None = None,
) -> str:
    """Run the shipped script with fake tools. Times in the fake outputs are `{now-N}` templates."""
    now = int(time.time()) if now is None else now
    fake, bin_, home = scratch / "fake", scratch / "bin", scratch / "home"
    for d in (fake, bin_, home):
        d.mkdir()

    def fill(s: str) -> str:
        return re.sub(r"\{ago(\d+)\}", lambda m: str(now - int(m[1])), s)

    files = {"panes": panes, "ps_a": ps_a, "ps_u": ps_u, "who": who, "ss": ""}
    if clients is None:
        (fake / "clients.err").write_text("no server running on /tmp/tmux-1000/default\n")
        (fake / "panes.err").write_text("no server running on /tmp/tmux-1000/default\n")
    else:
        files["clients"] = clients
    for name, text in files.items():
        (fake / name).write_text(fill(text))
    tools = {
        "tmux": FAKE_TMUX,
        "ps": FAKE_PS,
        "who": '#!/bin/sh\ncat "$FAKE/who"\n',
        "ss": '#!/bin/sh\ncat "$FAKE/ss"\n',
        "date": f"#!/bin/sh\necho {now}\n",
    }
    if awk == "busybox":
        tools["awk"] = '#!/bin/sh\nexec busybox awk "$@"\n'
    else:
        tools["awk"] = f'#!/bin/sh\nexec {shutil.which(awk)} "$@"\n'
    for name, text in tools.items():
        (bin_ / name).write_text(text)
        (bin_ / name).chmod(0o755)
    if uploads_age_s is not None:
        up = home / "Uploads"
        up.mkdir()
        (up / "design.bit").write_text("x")
        for p in (up / "design.bit", up):
            os.utime(p, (now - uploads_age_s, now - uploads_age_s))
    env = {
        "PATH": f"{bin_}:{os.environ['PATH']}",
        "HOME": str(home),
        "FAKE": str(fake),
        "FAKE_SSHD": str(os.getpid()),
        "SSH_CONNECTION": "1.2.3.4 5555 5.6.7.8 22",
        "LC_ALL": "C",
    }
    r = subprocess.run(["sh", "-c", inuse.remote_script()], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


def verdict_of(out: str) -> str:
    return out.rstrip("\n").splitlines()[-1]


@pytest.fixture(params=AWKS)
def awk(request):
    return request.param


def test_nothing_running_is_idle(scratch, awk):
    out = run_script(scratch, awk, clients=None)
    assert verdict_of(out) == "verdict: idle", out
    assert inuse.parse_script_output(out) == ("idle", [])


# --- tmux clients ----------------------------------------------------------------------


def test_tmux_client_key_press_under_300s_is_active(scratch, awk):
    out = run_script(scratch, awk, clients="{ago299} /dev/pts/0 default-0\n")
    assert "active: tmux client, 299 s ago" in out and verdict_of(out) == "verdict: in use", out


# --- unchanged: ssh terminals and ~/Uploads at 15 minutes, other logins always -----------------------

WHO = "pi       pts/1        2026-11-27 10:00 {idle}        4321 (9.9.9.9)\n"


@pytest.mark.parametrize(("idle", "active"), [("00:10", True), ("00:14", True), ("00:15", False), ("01:02", False)])
def test_ssh_terminal_stays_at_900s(scratch, awk, idle, active):
    out = run_script(scratch, awk, who=WHO.replace("{idle}", idle))
    assert ("active: ssh terminal" in out) == active, out


@pytest.mark.parametrize(("age", "active"), [(600, True), (899, True), (901, False)])
def test_uploads_stays_at_900s(scratch, awk, age, active):
    out = run_script(scratch, awk, uploads_age_s=age)
    assert ("active: ~/Uploads change" in out) == active, out


def test_command_for_another_login_is_always_in_use(scratch, awk):
    other = "7000 1 90000 sshd-session: pi@pts/3\n7001 7000 86000 minicom -D /dev/ttyUSB1\n"
    out = run_script(scratch, awk, ps_u=other)
    assert "  pid 7001, running 86000 s: minicom -D /dev/ttyUSB1" in out, out
    assert "active: 1 command(s) running for another login" in out and verdict_of(out) == "verdict: in use"
