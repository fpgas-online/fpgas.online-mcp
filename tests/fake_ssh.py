#!/usr/bin/env python3
"""Fake ssh for tests. Logs argv (JSON lines) to $FAKE_SSH_LOG, keeps master state in $FAKE_SSH_STATE.

Never reads or prints the password file.
"""

import json
import os
import subprocess
import sys
import time

argv = sys.argv[1:]
state = os.environ["FAKE_SSH_STATE"] + "." + next((a for a in argv if a.startswith("fpgas-")), "x")
with open(os.environ["FAKE_SSH_LOG"], "a") as log:
    askpass = os.environ.get("SSH_ASKPASS")
    log.write(
        json.dumps(
            {
                "argv": argv,
                "askpass_set": askpass is not None,
                "askpass_exists": bool(askpass) and os.path.exists(askpass),
                "askpass_require": os.environ.get("SSH_ASKPASS_REQUIRE"),
            }
        )
        + "\n"
    )

if os.environ.get("FAKE_SSH_FAIL_START") and "-MNf" in argv:
    sys.stderr.write(os.environ["FAKE_SSH_FAIL_START"] + "\n")
    sys.exit(255)
if "-O" in argv:
    op = argv[argv.index("-O") + 1]
    if op == "check":
        if os.path.exists(state):
            print("Master running (pid=1)", file=sys.stderr)
            sys.exit(0)
        print("Control socket connect: No such file or directory", file=sys.stderr)
        sys.exit(255)
    if op == "exit":
        if os.path.exists(state):
            os.remove(state)
            print("Exit request sent.", file=sys.stderr)
            sys.exit(0)
        sys.exit(255)
if "-MNf" in argv:
    open(state, "w").close()
    if os.fork() == 0:
        # Like a real daemonised master: new session, keeps the inherited stdout/stderr open.
        os.setsid()
        with open(state + ".child", "w") as f:
            f.write(str(os.getpid()))
        time.sleep(int(os.environ.get("FAKE_SSH_CHILD_S", "4")))
        os._exit(0)
    sys.exit(0)
if "--" in argv:
    command = argv[argv.index("--") + 1]
    sys.exit(subprocess.run(["sh", "-c", command], stdin=subprocess.DEVNULL).returncode)
sys.exit(2)
