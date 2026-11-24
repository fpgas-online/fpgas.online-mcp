# Is someone using this board?

Boards are shared. The people on them are humans using the web terminal on the board page,
and other agents using ssh. Before you power-cycle (power.md), load a design or overwrite
files, check that nobody else is on the board, and **skip a board that is in use**.

Full docs: https://docs.fpgas.online/en/latest/user/index.html

**A login is not use.** Hosts keep old ssh connections for days (other people's
`ControlPersist` masters, the site's own checks), so a count of logins says little. Look
for recent activity. The rule used below:

> Activity in the last **15 minutes (900 s)** means in use. Nothing for 15 minutes means idle.

Idle is not a promise: someone may be reading the screen or the camera. Tell the user which
board you are about to disturb.

## welland and ps1 (ssh hosts)

A person using the web terminal on a board page is a WebSSH session: an ssh login as `pi`
with a terminal. On ps1 hosts (`pi21` checked) `~/.zprofile` puts every interactive login
into one shared tmux session (windows named `default-N`), so a human shows up as an ssh
login plus a **tmux client**. welland's `pi-sw1-p2` had no `~/.zprofile` (login shell bash)
when checked on 2026-11-25, so there a human may show only as an ssh terminal. Your own
non-interactive `ssh host cmd` runs without a terminal and never makes a tmux client. Never
use `ssh -t`/`-tt`, never `exit` a shared window, never run `tmux kill-server` or
`kill-session` (ssh.md says why).

One command shows everything. Set up ssh first (ssh.md; `A` is the host's alias):

```sh
A=fo-ps1-pi3      # or fo-welland-pi-sw1-p2
ssh "$A" sh -s <<'EOF'
now=$(date +%s); win=900; unknown=
for c in ss tmux who find awk ps sort date; do
  command -v "$c" > /dev/null || { echo "missing tool on host: $c"; unknown=1; }
done
set -- $SSH_CONNECTION; me_ip=$1; me_port=$2
echo "you: $me_ip port $me_port"

echo "other ssh connections (address:port):"
ss -tnH state established '( sport = :22 )' | awk -v ip="$me_ip" -v port="$me_port" '
  { p=$4; sub(/^\[/,"",p); sub(/\]:/,":",p); want=ip ":" port
    if (substr(p, length(p)-length(want)+1) != want) print "  " p }' || unknown=1

t=$(tmux list-clients -F '#{client_activity} #{client_tty} #{session_name}' 2>&1) || {
  case "$t" in
    *"no server running"*|*"error connecting"*) echo "tmux: no clients ($t)" ;;
    *) echo "tmux: unexpected: $t"; unknown=1 ;;
  esac
  t=; }
echo "tmux clients:"
echo "$t" | awk -v now="$now" 'NF { print "  " $2, $3, "idle", now-$1, "s" }'
tmin=$(echo "$t" | awk -v now="$now" 'NF { i=now-$1; if (m=="" || i<m) m=i } END { print m }')
# a job left running in a tmux window (a build started in the web terminal) has no key presses
# and no sshd parent: list windows running anything but a shell or the help window's tail
pb=$(tmux list-panes -a -F '#{pane_current_command}' 2>&1) || pb=
pb=$(echo "$pb" | awk 'NF && $1 !~ /^-?(zsh|bash|sh|tail)$/ { print "  " $1 }')
echo "tmux windows running a program:"
[ -n "$pb" ] && echo "$pb"

# terminal logins: find the pts/N field, then the idle field (the last one that looks like
# `.`, `old` or H:MM; the login time is earlier on the line)
w=$(who -u | awk '{ for (k=1; k<=NF; k++) if ($k ~ /^pts\//) break
  if (k>NF) next
  for (j=NF; j>k; j--) if ($j ~ /^(\.|old|[0-9]+:[0-9]+)$/) break
  if (j<=k) { print "? " $k " " $NF; next }
  i=0; if ($j=="old") i=999999; else if ($j ~ /:/) { split($j,a,":"); i=a[1]*3600+a[2]*60 }
  print i, $k, $NF }')
echo "ssh terminals (who -u):"
echo "$w" | awk 'NF { print "  " $2, $3, "idle >=", $1, "s" }'
echo "$w" | awk '$1 == "?" { f=1 } END { exit !f }' && unknown=1
wmin=$(echo "$w" | awk '$1 != "?" && NF { if (m=="" || $1<m) m=$1 } END { print m }')

umin=
if [ -d "$HOME/Uploads" ]; then
  u=$(find "$HOME/Uploads" -printf '%T@\n' | sort -n | tail -n 1)
  [ -n "$u" ] && umin=$((now - ${u%.*}))
fi
echo "newest change in ~/Uploads: ${umin:-none} s ago"

# commands running for other ssh logins (an agent's `ssh host 'flash...'`): any process that
# is a direct child of an sshd login other than yours, apart from tmux (counted above) and
# sftp-server older than 15 min (an idle file-transfer session, not a running command)
echo "commands running for other logins:"
procs=$(ps -u "$(id -u)" -o pid=,ppid=,etimes=,args= | awk -v me="$$" -v win="$win" '
  { pid=$1; ppid[pid]=$2; et[pid]=$3; a=$0; sub(/^ *[0-9]+ +[0-9]+ +[0-9]+ +/,"",a); args[pid]=a }
  END { r=me; n=0
        while (r != "" && r != 0 && args[r] !~ /^sshd(-session)?: [a-z]+@/ && n++ < 50) r=ppid[r]
        if (args[r] !~ /^sshd(-session)?: [a-z]+@/) { print "?"; exit }
        for (q in ppid) if (ppid[q] != r && args[ppid[q]] ~ /^sshd(-session)?: [a-z]+@/ && args[q] !~ /^tmux/ && !(args[q] ~ /sftp-server/ && et[q] >= win))
          print "  pid " q ", running " et[q] " s: " substr(args[q],1,70) }')
if [ "$procs" = "?" ]; then echo "  could not find your own login in the process list"; unknown=1; procs=; fi
[ -n "$procs" ] && echo "$procs"
others=$(echo "$procs" | awk 'NF { n++ } END { print n+0 }')

verdict=idle
for kv in "tmux client:$tmin" "ssh terminal:$wmin" "~/Uploads change:$umin"; do
  v=${kv#*:}
  if [ -n "$v" ] && [ "$v" -lt "$win" ]; then verdict="in use"; echo "active: ${kv%%:*}, $v s ago"; fi
done
if [ "$others" -gt 0 ]; then verdict="in use"; echo "active: $others command(s) running for another login"; fi
if [ -n "$pb" ]; then verdict="in use"; echo "active: a program running in a tmux window"; fi
if [ "$verdict" = idle ] && [ -n "$unknown" ]; then verdict="unknown (treat as in use)"; fi
echo "verdict: $verdict"
EOF
```

It reuses your master connection from ssh.md (the first call opens it). It prints these parts:

- `you: ...` is your own client address and source port as the host sees them
  (`$SSH_CONNECTION`).
- `other ssh connections` lists every established ssh connection **except yours**, matched
  on address **and** port. The port is what separates you from others when everyone shares
  one address (see limits). Entries in this list are not use by themselves. On `pi21` six
  old connections from the site gateway were listed with nothing running in them.
- `tmux clients` lists attached terminals with their idle seconds (the key-press clock of
  that client). `tmux: no clients (...)` means tmux has no server: no interactive terminal is
  open (the server exits when the last one closes).
- `tmux windows running a program` lists tmux windows whose program is not a shell (`zsh`,
  `bash`, `sh`) or the help window's `tail`: a build or capture someone started in the web
  terminal and left running. It has no key presses and no sshd parent, so no other signal
  sees it once it runs past 15 minutes. Any line here means in use.
- `ssh terminals` lists logins that have a terminal, with idle time from `who -u` (accurate
  to the minute; `old` counts as a day; the script finds the idle field by its shape, so a
  different column layout is tolerated). This covers a terminal that is not in tmux.
- `newest change in ~/Uploads` is the age of the newest file, directory or deletion there
  (someone copying files or building).
- `commands running for other logins` lists processes started by another ssh login than
  yours (any command a second agent runs with `ssh host 'openFPGALoader ...'`, a serial
  capture, a build), with how long each has run. These need no terminal and write nothing to
  `~/Uploads`, so this is the only signal for them. tmux is skipped (counted above) and so
  is an `sftp-server` older than 15 minutes (an idle file-transfer session, such as the
  site's). Any command listed means in use, however long it has run.
- The verdict line: `in use` with one `active:` line per signal (under 900 s, or any command
  above), `unknown (treat as in use)` when the script could not check something (a tool is
  missing on the host, tmux said something unexpected, a `who` line did not parse, or it
  could not find your own login), otherwise `idle`. Treat `unknown` like `in use`: ask the
  user.

An idle host prints (measured on ps1 `pi21`; the six lines are old connections from the
site gateway, listed but not use; `::ffff:` is how the host writes an IPv4 address):

```
you: 87.121.95.37 port 43804
other ssh connections (address:port):
  ::ffff:<gateway address>:57646
  ... five more lines like it
tmux: no clients (no server running on /tmp/tmux-1000/default)
tmux clients:
tmux windows running a program:
ssh terminals (who -u):
newest change in ~/Uploads: 5808 s ago
commands running for other logins:
verdict: idle
```

The `tmux: no clients (...)` line is not a failure: the message is `no server running on
...` or `error connecting to ... (No such file or directory)` (the latter on `pi-sw1-p2`,
where nobody had logged in). Any other tmux message gives `unknown`. A tmux server whose
windows ran `sleep`, a login shell and `tail` (a private test server on `pi-sw1-p2`) gave
`tmux windows running a program:` then `  sleep`, and `active: a program running in a tmux
window`. The old `sftp-server`
sessions of the gateway are not listed (they were, before the 15-minute exception).

Another agent running `ssh host 'sleep 60'` (a separate non-interactive connection, no
terminal; measured on `pi21`) prints:

```
other ssh connections (address:port):
  ::ffff:87.121.95.37:54628
tmux: no clients (no server running on /tmp/tmux-1000/default)
tmux clients:
tmux windows running a program:
ssh terminals (who -u):
newest change in ~/Uploads: 5820 s ago
commands running for other logins:
  pid 11793, running 10 s: sleep 60
active: 1 command(s) running for another login
verdict: in use
```

If the connection list holds an address that is neither the gateway nor yours and nothing
else explains it, ask the user before touching the board, even when the verdict is `idle`:
a connection with a quiet command (for example a long `scp` through an old master) can slip
past the checks.

A second person on the board prints (a **test demonstration; do not repeat it**: a login
with `ssh -tt`, which joins tmux, twelve seconds after typing a command; measured before the
`commands running` and `tmux windows` parts existed, which list nothing here: tmux is skipped
and the window is back at its shell; the second user shares your address, so only the port
tells them apart):

```
other ssh connections (address:port):
  ::ffff:87.121.95.37:54438
tmux clients:
  /dev/pts/0 default-0 idle 12 s
tmux windows running a program:
ssh terminals (who -u):
  pts/0 (87.121.95.37) idle >= 0 s
newest change in ~/Uploads: 5495 s ago
commands running for other logins:
active: tmux client, 12 s ago
active: ssh terminal, 0 s ago
verdict: in use
```

A terminal login that is not in tmux (`ssh -tt host 'sleep 25'`, also a test demonstration
not to repeat) shows only the `ssh terminals` line and `active: ssh terminal`. Read the last line; the script's exit
status is not used.

### Recognising the website's terminal

The web terminal's ssh logins all come from the site's gateway (the WebSSH proxy), so they
share one source address that is neither yours nor another user's: it is the address that
repeats in the `other ssh connections` list and in the `who` host column (on ps1 `pi21`
every web-terminal and site-tool connection came from one address, and it was not mine). A
terminal login from that address with a tmux client is a person on the board page. The same address also appears on terminal-less connections made
by the site's own tooling, which is why only terminal (tmux/`pts`) activity and `~/Uploads`
count, not the connection list.

### Limits

- **Your own address may not be unique.** From inside welland's network the host may see
  the router's address for you and for everyone else inside, so addresses cannot tell you
  from them; the source **port** of your master connection does, and the command excludes
  exactly that address and port. It does not cover a second ssh process of yours that is
  not the master (a separate `ssh -tt` of yours is a different port and counts as another
  user, correctly, if you keep it open).
- **One shared shell.** All interactive logins share one tmux window and one shell, so a
  person on the web terminal sees what an agent types there and the reverse. Typing `exit`
  there ended that window for everyone in testing (the tmux server went away, closing every
  attached terminal). Use non-interactive `ssh host cmd` for your own work.
- **Someone only watching** the web page, the camera, or a tmux window that has not been
  touched for 15 minutes is not detected. A viewer who is reading is not "using" the
  board by the activity rule.
- **A long job in the web terminal** (a 20-minute build watched on the camera) is seen only
  through `tmux windows running a program`: its shell is a child of the tmux server, not of
  sshd, and nobody presses keys. A job started outside tmux in a plain terminal shows only as
  that terminal's `who` idle time, which passes 15 minutes.
- **Nothing shows web-page viewers** on welland or ps1: the page itself opens no
  connection until the terminal panel runs.
- **No claim or reservation system** exists yet (the MCP server is planned to add one).
  The `/run/fpgas-online/` files on welland hosts (`arty.lock`, `fomu.lock`, `netv2.lock`,
  `tt-fpga.lock`, `verify.json`) belong to the host's own board check, are root-owned and
  empty, and were not held when looked at; they are not a way to claim a board. ps1 hosts
  have no `/run/fpgas-online/` at all. Do not rely on them.
- **Quiet non-interactive work is only partly seen.** A second agent's command shows while it
  runs (the `commands running` signal), but an old master of theirs that is only holding a
  transfer or forward open, a command that has finished and left results to be collected, or
  a file transfer through a connection older than 15 minutes are not. Ask the user when the
  connection list holds something unexplained.
- A host that does not answer ssh cannot be checked: say so and ask the user. A missing
  tool or an unreadable `who` gives `verdict: unknown`, not `idle`.

## tinytapeout

No ssh. The board's own daemon reports who is attached. One GET:

```sh
S=fpga-2
curl -sS -A fpgas-online-skill "https://tinytapeout.fpgas.online/board/$S/status.json" |
  jq -c '{reachable, clients, idle: .idle_display.state}'
```

For a connected, idle board it prints `{"reachable":true,"clients":0,"idle":"loaded"}`; for a
slug with no board `{"reachable":false,"clients":null,"idle":null}` (the response is
`{"reachable": false, "error": "not connected"}`).

- `clients` is the number of open serial connections to the board. A person on the board
  page's terminal or live view counts, and so does any agent with a serial connection. It is
  a live count but **lags**: the site caches `status.json` for 5 s and the page polls every
  10 s. It briefly also counts the daemon's own REPL connection and an HTTP enable (a false
  "in use", which is safe), and a visitor whose WebSocket arrives while the daemon has the
  board is held, not counted, for up to 20 s (the state then says `in use`).
- `idle_display.state` (from the daemon's code; the quiet time is 60 s): `loaded` means the
  idle display is on the board and nothing has happened for at least 60 s. `waiting` means
  the board **was used less than 60 s ago** (a WebSocket closed, an enable or upload ended):
  someone may be about to come back, so it is not idle; wait about 75 s and read again.
  `in use` means a serial client is connected or a load is running. `left: <design>` means a
  design somebody loaded is still there and will not be replaced: they may be done or about
  to come back; treat it as in use and ask the user. `left: the SDK is not running` is the
  state the Pi's boot check leaves (no SDK started); treat it the same way. `board not
  present` and `failed: ...` are not idle either. A chip board always says `not an fpga
  board` (checked live), so it is never idle by this rule: ask the user before touching one.
  `idle_display` is `null` only when the board is not reachable.
- **Idle** is `reachable: true`, `clients: 0` and `idle_display.state` `loaded` on two reads
  at least 15 s apart. Anything else is not idle.

Nothing is changed by these GETs; poll at most every 10 s.

## What to do

- In use: do not cycle, do not load, do not write to `~/Uploads`. Pick another host
  (boards.md) or tell the user who and since when (the `tmux clients` line).
- Idle: go ahead, and tell the user. Run the check again just before a destructive step.
- Unsure: ask.
