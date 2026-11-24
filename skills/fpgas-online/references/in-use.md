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

A person using the web terminal on a board page is a WebSSH session. It logs in over ssh as
`pi`, with a terminal, and `~/.zprofile` on the host puts every interactive login into one
shared tmux session (windows named `default-N`). So on the host a human shows up as an
ssh login plus a **tmux client**. Your own non-interactive `ssh host cmd` runs without a
terminal and never makes a tmux client. (ssh.md: do not use `ssh -t` unless you want to
join that session.)

One command shows everything. Set up ssh first (ssh.md; `A` is the alias, here
`fo-ps1-pi21`; welland `fo-welland-pi-sw1-p2` works the same):

```sh
A=fo-ps1-pi21
ssh "$A" sh -s <<'EOF'
now=$(date +%s); win=900
set -- $SSH_CONNECTION; me_ip=$1; me_port=$2
echo "you: $me_ip port $me_port"

echo "other ssh connections (address:port):"
ss -tnH state established '( sport = :22 )' | awk -v ip="$me_ip" -v port="$me_port" '
  { p=$4; sub(/^\[/,"",p); sub(/\]:/,":",p); want=ip ":" port
    if (substr(p, length(p)-length(want)+1) != want) print "  " p }'

t=$(tmux list-clients -F '#{client_activity} #{client_tty} #{session_name}' 2>&1) || { echo "tmux: no clients ($t)"; t=; }
echo "tmux clients:"
echo "$t" | awk -v now="$now" 'NF { print "  " $2, $3, "idle", now-$1, "s" }'
tmin=$(echo "$t" | awk -v now="$now" 'NF { i=now-$1; if (m=="" || i<m) m=i } END { print m }')

w=$(who -u | awk '$3 ~ /^pts\// { i=($6=="." ? 0 : ($6=="old" ? 999999 : 0)); if ($6 ~ /:/) { split($6,a,":"); i=a[1]*3600+a[2]*60 }; print i, $3, $NF }')
echo "ssh terminals (who -u):"
echo "$w" | awk 'NF { print "  " $2, $3, "idle >=", $1, "s" }'
wmin=$(echo "$w" | awk 'NF { if (m=="" || $1<m) m=$1 } END { print m }')

u=$(find "$HOME/Uploads" -printf '%T@\n' | sort -n | tail -n 1)
umin=; [ -n "$u" ] && umin=$((now - ${u%.*}))
echo "newest change in ~/Uploads: ${umin:-none} s ago"

verdict=idle
for kv in "tmux client:$tmin" "ssh terminal:$wmin" "~/Uploads change:$umin"; do
  v=${kv#*:}
  if [ -n "$v" ] && [ "$v" -lt "$win" ]; then verdict="in use"; echo "active: ${kv%%:*}, $v s ago"; fi
done
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
- `ssh terminals` lists logins that have a terminal, with idle time from `who -u` (accurate
  to the minute; `old` counts as a day). This covers a terminal that is not in tmux.
- `newest change in ~/Uploads` is the age of the newest file, directory or deletion there
  (someone copying files or building).
- The verdict line: `in use` with one `active:` line per signal under 900 s, otherwise
  `idle`.

An idle host prints (measured on ps1 `pi21`; the six lines are old connections from the
site gateway, listed but not use; `::ffff:` is how the host writes an IPv4 address):

```
you: 87.121.95.37 port 56060
other ssh connections (address:port):
  ::ffff:<gateway address>:57646
  ... five more lines like it
tmux clients:
ssh terminals (who -u):
newest change in ~/Uploads: 5376 s ago
verdict: idle
```

When no tmux server runs (always true on `pi-sw1-p2`, which nobody had logged in to) the
line after the connection list reads `tmux: no clients (error connecting to
/tmp/tmux-1000/default (No such file or directory))`; that is idle, not a failure.

A second person on the board prints (a login with `ssh -tt`, which joins tmux, three
seconds after typing a command; the second user shares your address here, so only the port
tells them apart):

```
other ssh connections (address:port):
  ::ffff:87.121.95.37:54438
tmux clients:
  /dev/pts/0 default-0 idle 12 s
ssh terminals (who -u):
  pts/0 (87.121.95.37) idle >= 0 s
newest change in ~/Uploads: 5495 s ago
active: tmux client, 12 s ago
active: ssh terminal, 0 s ago
verdict: in use
```

A terminal login that is not in tmux (`ssh -tt host 'sleep 25'`) shows only the
`ssh terminals` line and `active: ssh terminal`. Read the last line; the script's exit
status is not used.

### Recognising the website's terminal

The web terminal's ssh logins all come from the site's gateway (the WebSSH proxy), so they
share one source address that is neither yours nor another user's: it is the address that
repeats in the `other ssh connections` list and in the `who` host column, (on ps1 `pi21` every web-terminal and site-tool connection came from one address, and
it was not mine). A terminal login from that address with a tmux client is
a person on the board page. The same address also appears on terminal-less connections made
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
  there ends that window for everyone (the tmux server went away in testing, closing
  every attached terminal). Never `exit` it, never type into it while the verdict is
  `in use`, and use non-interactive `ssh host cmd` for your own work.
- **Someone only watching** the web page, the camera, or a tmux window that has not been
  touched for 15 minutes is not detected. A viewer who is reading is not "using" the
  board by the activity rule.
- **Nothing shows web-page viewers** on welland or ps1: the page itself opens no
  connection until the terminal panel runs.
- **No claim or reservation system** exists yet (the MCP server is planned to add one).
  The `/run/fpgas-online/` files on welland hosts (`arty.lock`, `fomu.lock`, `netv2.lock`,
  `tt-fpga.lock`, `verify.json`) belong to the host's own board check, are root-owned and
  empty, and were not held when looked at; they are not a way to claim a board. ps1 hosts
  have no `/run/fpgas-online/` at all. Do not rely on them.
- A host that does not answer ssh, or has `tmux` missing, cannot be checked this way: say so
  and ask the user rather than guessing.

## tinytapeout

No ssh. The board's own daemon reports who is attached. One GET:

```sh
S=fpga-2
curl -sS -A fpgas-online-skill "https://tinytapeout.fpgas.online/board/$S/status.json" |
  jq -c '{reachable, clients, idle: .idle_display.state}'
```

For a connected board it prints `{"reachable":true,"clients":0,"idle":"loaded"}`; for a slug
with no board `{"reachable":false,"clients":null,"idle":null}` (the response is
`{"reachable": false, "error": "not connected"}`).

- `clients` is the number of open serial connections to the board. A person on the board
  page's terminal or live view counts, and so does any agent with a serial connection. `0`
  means nobody is attached right now. Unlike the 15-minute rule above it is a live count,
  but it **lags**: the site caches `status.json` for 5 s and the page itself polls every
  10 s, so read it twice, at least 15 s apart, and treat `clients > 0` on either read as in
  use.
- `idle_display.state` says what the board is showing: `loaded` (the idle display, nobody
  has used the board for a while), `waiting` (the daemon is waiting out its quiet time),
  `in use` (someone is using it), `left: <a visitor's design>` (somebody's design is still
  loaded and is not replaced: they may be done, or about to come back; treat as in use and
  ask the user), `board not present`, `not an fpga board`, `failed: ...`. `idle_display` is
  `null` on a board that is not an FPGA board.
- Idle is `clients: 0` on both reads and a state of `loaded` or `waiting`. Anything else is
  not idle.

Nothing is changed by these GETs; poll at most every 10 s.

## What to do

- In use: do not cycle, do not load, do not write to `~/Uploads`. Pick another host
  (boards.md) or tell the user who and since when (the `tmux clients` line).
- Idle: go ahead, and tell the user. Run the check again just before a destructive step.
- Unsure: ask.
