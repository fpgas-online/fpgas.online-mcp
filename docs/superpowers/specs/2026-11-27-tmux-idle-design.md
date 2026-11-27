# tmux with no key presses for 5 minutes is idle: design

Date: 2026-11-27. Status: approved by the user. Asked "Should a tmux session on a Pi that has had no
key presses for a long time count as idle, and after how long?", the user answered "Yes, 5 minutes."
This follows the user's rule for Tiny Tapeout boards: a board is in use when someone is actively
interacting with it, not when something was left on it.

## Today (`src/fpgas_online_mcp/data/in_use.sh`, the same block in `skills/fpgas-online/references/in-use.md`)

- A tmux client counts as in use when its last key press (`#{client_activity}`) is less than
  `win=900` s ago.
- "a program running in a tmux window" (a pane whose process has a child or is not a shell / the
  help window's tail) is **always** in use, whatever the key presses: a forgotten `minicom` or
  `htop` keeps a host "in use" for days.
- ssh terminals (`who -u` idle) and `~/Uploads` changes use the same 900 s; commands running for
  other ssh logins are always in use.

## Change

1. **tmux key presses: 5 minutes.** A tmux client is active when its last key press is less than
   300 s ago (a new `tmux_win=300`; `win=900` stays for ssh terminals, `~/Uploads` and the
   sftp-server rule).
2. **A program in a tmux window counts only with recent key presses.** It is in use only when its
   session had a key press less than 300 s ago. Use the last key press of the session (check what
   tmux records: `#{session_activity}` and/or the most recent `#{client_activity}` of clients
   attached to that session; read the tmux man page for the exact meaning, it must be input, not
   output, since `htop` redraws constantly). Otherwise it is not in use, but the script still lists
   it, and the verdict carries a signal such as `tmux window pid 123 (minicom): no key press for
   18 min`, so the agent can tell the user what it will disturb.
3. **Unchanged:** ssh terminals and `~/Uploads` (900 s), commands running for another ssh login
   (always in use), the unknown rules, the holder's `~/Uploads` discount. If tmux cannot say when a
   session last had a key press, the program counts as in use (unknown is never idle).
4. **The script and the skill stay identical**: `in-use.md` carries the same script block (a test
   compares them). Update the prose there (what counts as active, the 5 minutes), the tool
   descriptions if they mention 15 minutes for tmux, and the 2026-11-25 spec's in-use section.
5. **Parser** (`inuse.parse_script_output`): reads any new script lines (e.g. the idle tmux
   windows) into signals; an idle-but-running tmux program is a signal on an `idle` verdict.

## Testing

Unit tests on `parse_script_output` with script output for each case, and a shell test of the
script's awk logic if the repo has a pattern for it (look at how the script is tested now). No ssh
to any real board.
