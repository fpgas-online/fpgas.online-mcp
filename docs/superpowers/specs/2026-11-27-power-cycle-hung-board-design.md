# power_cycle for a hung board: design

Date: 2026-11-27. Status: approved by the user ("(1) Yes") to this proposal: allow the cycle when the
in-use verdict is unknown (never "in use") and the claim carries an `override_reason`; for a board
that was unreachable, count it back once it answers with a fresh boot.

## Why

`power_cycle` cannot recover a hung board, the case it exists for (reported by another session on
2026-11-27, cycling stuck welland Tiny Tapeout boards after an NFS-root deploy):

1. A Pi that is down reads `unknown` (ssh: "No route to host"; site: not reachable), so
   `claim_board` needs `override_reason`, which the user gave.
2. `power_cycle` then refuses anyway: "not cycling: the in-use check says unknown".
3. Even past that, it refuses when it cannot read the board's current boot id / `uptime_s`
   ("a reboot could not be proven"), and a dead board has neither.

The session fell back to the site's toggle by hand, outside the broker.

## Design

1. **The in-use gate.** `power_cycle` still runs its fresh in-use check. `in use` always refuses
   (unchanged). `unknown` refuses unless the claim has an `override_reason`; with one it goes on, with
   the step `in-use check: unknown; going ahead on the claim's override (<reason>)`. `idle` unchanged.
   The override is the claim's own (recorded at claim time, shown to everyone); `power_cycle` takes
   no override argument.
2. **The boot proof when the old boot cannot be read.** Only under an override (1): when the old
   boot id (welland/ps1: ssh, then registry) or old `uptime_s` (Tiny Tapeout) cannot be read, record
   the step `board not answering before the toggle: back means it answers with a boot younger than
   the toggle` and go on (instead of refusing). Without an override it refuses as now.
   Then the board is back when:
   - Tiny Tapeout: `uptime_s` readable and smaller than the time since the toggle (the existing
     test, which needs no old value);
   - welland / ps1: over ssh, the host's uptime (`/proc/uptime`, first field) is smaller than the
     time since the toggle, read in the same probe as the boot id. Never "it answers" alone.
   When the old value was read, the proof is unchanged (boot id changed / uptime smaller).
3. **Everything else unchanged**: claim checks (live claim, host match for Tiny Tapeout, the
   allow_unoffered gate), closing the ssh master before the toggle, no retry on 429, polling every
   10 s for up to 300 s, the not-PoE message (only meaningful when the old boot was known: with no
   old value, a board that never answers gives the "did not come back" message).
4. **Messages** tell the agent what happened and what to do; the result's `steps` show the override
   and the missing old value.
5. **Docs.** Skill `references/power.md`, the `power_cycle` tool description, README: a board that is
   down reads unknown; claim it with `override_reason` (only when the user agrees) and power_cycle
   will cycle it; how "back" is proven then.

## Non-goals

No change to `claim_board`'s rules. No cycling of a board that reads `in use`.
