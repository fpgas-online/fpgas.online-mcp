A=fo-ps1-pi3      # or fo-welland-pi-sw1-p2
ssh "$A" sh -s <<'EOF'
now=$(date +%s); win=900; tmux_win=300; unknown=
for c in ss tmux who find awk ps sort date; do
  command -v "$c" > /dev/null || { printf 'missing tool on host: %s\n' "$c"; unknown=1; }
done
set -- $SSH_CONNECTION; me_ip=$1; me_port=$2
printf 'you: %s port %s\n' "$me_ip" "$me_port"

echo "other ssh connections (address:port):"
ss -tnH state established '( sport = :22 )' | awk -v ip="$me_ip" -v port="$me_port" '
  { p=$4; sub(/^\[/,"",p); sub(/\]:/,":",p); want=ip ":" port
    if (substr(p, length(p)-length(want)+1) != want) print "  " p }' || unknown=1

t=$(tmux list-clients -F '#{client_activity} #{client_tty} #{session_name}' 2>&1) || {
  case "$t" in
    *"no server running"*|*"error connecting"*) printf 'tmux: no clients (%s)\n' "$t" ;;
    *) printf 'tmux: unexpected: %s\n' "$t"; unknown=1 ;;
  esac
  t=; }
echo "tmux clients:"
printf '%s\n' "$t" | awk -v now="$now" 'NF { print "  " $2, $3, "idle", now-$1, "s" }'
tmin=$(printf '%s\n' "$t" | awk -v now="$now" 'NF { i=now-$1; if (m=="" || i<m) m=i } END { print m }')
# a job left running in a tmux window (a build started in the web terminal) has no sshd parent:
# a window is busy when its process has a child (a command, a script, a `job &`) or is itself not
# a shell or the help window's tail. It counts while its session had a key press in the last
# $tmux_win s (#{session_activity}: key presses and attaching move it, output does not), or when
# tmux gives no such time; otherwise it is listed as idle with the minutes since the last key press
pp=$(tmux list-panes -a -F '#{pane_pid} #{session_activity} #{pane_current_command}' 2>&1) || pp=
pw=$( { printf '%s\n' "$pp" | awk 'NF { print "P", $0 }'; ps -A -o ppid=,args=; } | awk -v now="$now" -v win="$tmux_win" '
  $1 == "P" { if ($2 !~ /^[0-9]+$/) next
              p=$2; a="?"; if (NF > 3 && $3 ~ /^[0-9]+$/ && $3 > 0) a=$3
              if (NF > 3) cmd[p]=$4; else cmd[p]=$3
              if (!(p in act) || a == "?" || (act[p] != "?" && a+0 > act[p]+0)) act[p]=a
              next }
  ($1 in cmd) { a=$0; sub(/^ *[0-9]+ +/,"",a); kid[$1]=a }
  END { for (p in cmd) if ((p in kid) || cmd[p] !~ /^-?(zsh|bash|sh|tail)$/) {
          what=cmd[p]; if (p in kid) what=substr(kid[p],1,60)
          if (act[p] != "?" && now-act[p] >= win)
            print "I  window pid " p " (" cmd[p] "), no key press for " int((now-act[p])/60) " min: " what
          else print "B  window pid " p " (" cmd[p] "): " what } }')
pb=$(printf '%s\n' "$pw" | awk 'sub(/^B/, "")')
pq=$(printf '%s\n' "$pw" | awk 'sub(/^I/, "")')
echo "tmux windows running a program:"
[ -n "$pb" ] && printf '%s\n' "$pb"
printf 'tmux windows running a program, idle (no key press for %s s):\n' "$tmux_win"
[ -n "$pq" ] && printf '%s\n' "$pq"

# terminal logins: find the pts/N field, then the idle field (the last one that looks like
# `.`, `old` or H:MM; the login time is earlier on the line)
w=$(who -u | awk '{ for (k=1; k<=NF; k++) if ($k ~ /^pts\//) break
  if (k>NF) next
  for (j=NF; j>k; j--) if ($j ~ /^(\.|old|[0-9]+:[0-9]+)$/) break
  if (j<=k) { print "? " $k " " $NF; next }
  i=0; if ($j=="old") i=999999; else if ($j ~ /:/) { split($j,a,":"); i=a[1]*3600+a[2]*60 }
  print i, $k, $NF }')
echo "ssh terminals (who -u):"
printf '%s\n' "$w" | awk 'NF { print "  " $2, $3, "idle >=", $1, "s" }'
printf '%s\n' "$w" | awk '$1 == "?" { f=1 } END { exit !f }' && unknown=1
wmin=$(printf '%s\n' "$w" | awk '$1 != "?" && NF { if (m=="" || $1<m) m=$1 } END { print m }')

umin=
if [ -d "$HOME/Uploads" ]; then
  u=$(find "$HOME/Uploads" -printf '%T@\n' | sort -n | tail -n 1)
  [ -n "$u" ] && umin=$((now - ${u%.*}))
fi
printf 'newest change in ~/Uploads: %s s ago\n' "${umin:-none}"

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
[ -n "$procs" ] && printf '%s\n' "$procs"
others=$(printf '%s\n' "$procs" | awk 'NF { n++ } END { print n+0 }')

verdict=idle
if [ -n "$tmin" ] && [ "$tmin" -lt "$tmux_win" ]; then verdict="in use"; printf 'active: tmux client, %s s ago\n' "$tmin"; fi
for kv in "ssh terminal:$wmin" "~/Uploads change:$umin"; do
  v=${kv#*:}
  if [ -n "$v" ] && [ "$v" -lt "$win" ]; then verdict="in use"; printf 'active: %s, %s s ago\n' "${kv%%:*}" "$v"; fi
done
if [ "$others" -gt 0 ]; then verdict="in use"; printf 'active: %s command(s) running for another login\n' "$others"; fi
if [ -n "$pb" ]; then verdict="in use"; echo "active: a program running in a tmux window"; fi
if [ "$verdict" = idle ] && [ -n "$unknown" ]; then verdict="unknown (treat as in use)"; fi
printf 'verdict: %s\n' "$verdict"
EOF
