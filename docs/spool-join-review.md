# Spool Join Review (INDX v1.0-beta.5, checked again on beta.6 and beta.7)

Review of INDX's built-in endless spool ("spool join": `klipper_extras/indx_remap.py`, `macros/indx_suite_remap.cfg`, `PRINT_TOOLCHANGE`) on Kalico 991e182. Updated from the beta.3 review.

> **Beta.7 (2026-11-16): findings 1 to 3 are fixed.** Beta.7's changelog lists them ("Spool join: the printer could wait for ever"), its code matches the proposed patch, and the reproduction script reports no bug on it. The bridge's workarounds stand aside on beta.7. Finding 4 is now moot in a different way: beta.7 leaves every runout to a registered tool manager, and the bridge keeps INDX's spool join deciding.
>
> **Beta.6 (2026-11-12).** `indx_remap.py` is unchanged where these findings are: findings 1 to 4 still hold, and the patch applies to beta.6 as it is. Beta.6's changelog has no spool join changes.

> **Corrections to the beta.3 review (2026-11-11).**
> - Finding 1 ("runouts may never reach the spool join") is **withdrawn for Kalico**. Kalico's `filament_switch_sensor` sends `filament:runout` and `filament:insert`, which INDX listens for. It holds only for mainline Klipper, where INDX does not run its Suite anyway.
> - The earlier advice to use `runout_gcode: INDX_RUNOUT DOCK=<n>` stays wrong: see finding 1.

## How It Works

- `[indx_remap] runout_sensors:` names one sensor per dock.
- A runout on the tool printing switches at once to a partner and returns to the exact X/Y/Z and E state.
- A runout on a parked tool moves its slicer tools to the partner. While paused, the switch runs after RESUME. No partner: PAUSE.
- Partner: same base material, same nozzle size, both colors recorded and within RGB distance 150, its own sensor not empty.
- Off whenever any tool manager is registered, even one that does not own `T<n>`.

New in beta.5: a runout during a tool change is held until the change ends (`join_busy`), and a second runout during a join is queued (`join_next`).

## Findings

### 1. INDX_RUNOUT waits on its own lock (high)

`cmd_RUNOUT` calls `_runout` inside the G-code command. `_runout` runs `PAUSE` or `_INDX_SPOOL_JOIN` with `gcode.run_script`, which takes Kalico's G-code lock; the running command already holds it and the lock is not re-entrant, so the command never returns. The sensor event path defers through a reactor callback and is fine. A console `INDX_RUNOUT DOCK=<n>` in a print, or a sensor's `runout_gcode: INDX_RUNOUT ...`, takes the command path. Reproduced against the real `indx_remap.py` with a model of the lock.

Fix: defer `_runout` with `reactor.register_callback`, as `_ev_runout` does.

### 2. The queued join waits on the same lock (high, new in beta.5)

`cmd_JOINED` (the command `_INDX_REMAP_JOINED`, run at the end of a join) starts a queued second join with `gcode.run_script("_INDX_SPOOL_JOIN")`, inside the command. Same hang, mid-print, when a partner's spool runs out during the first join. The queued record also holds the position captured on the way back from the dock, not the print point.

Fix: defer `_start_join(from, to)` to a callback after the command; it then captures the print point fresh.

### 3. A runout during the pickup of the empty tool prints on empty (medium, new in beta.5)

When the runout arrives during a tool change, `_runout` stores `join_busy` if the empty tool is the active one. `_watch` resolves it after the change by remapping only, on the comment's assumption that "the tool is in its dock". That holds when the empty tool was being parked. When it was being picked up, it is on the head and printing, and nothing switches until the slicer's next change.

Fix: in `_watch`, if the tool is the active one and the print is running, start the join instead of only remapping.

### 4. A feeder-type tool manager turns the spool join off (design)

`cmd_PLAN` and `_ready` test `self._manager()`. A manager with `owns_tool_commands: False` (a filament system feeding the tools, INDX keeping the tool changes) loses mapping and spool join for no reason. Fix: test `owns_tool_commands`.

### 5. Motion sensors on parked tools (high, if motion sensors are used)

Unchanged from beta.3. INDX has one `[extruder]` for every dock, so a parked dock's `[filament_motion_sensor]` sees the extruder move with no filament motion and reads empty. Use switch sensors.

### 6. The partner is never primed or cleaned (medium)

Unchanged. A partner the print did not list is not primed, cleaned or Z-checked at print start, and a join skips the purge station. beta.5's `INDX_PRIME_TEST` helps by hand before the print.

### 7. A print not started with INDX_PRINT_START has no spool join (low)

Unchanged; the pause reason given ("a tool manager owns the tools") is wrong in that case.

### 8. Switch timing (enhancement)

Unchanged. The join switches the moment the spool-side sensor reads empty, leaving the sensor-to-gears length in the tool. With a Box Turtle that is the whole `dist_hub` (often 800 mm or more). An optional `join_after_mm` would use it up.

## With the INDX Bridge (unofficial; called indx-afc before 0.3.0)

The bridge (v0.2.1) works around findings 1 to 4 at runtime without changing INDX's files: it wraps `INDX_RUNOUT` and `_INDX_REMAP_JOINED` to defer, wraps `_watch` to start the held join, and tells `indx_remap` that its manager only feeds the tools. It hands each lane's prep sensor to the spool join, so no `runout_sensors:` line is needed.

`patches/indx-remap-beta5-fixes.patch` in the bridge holds findings 1 to 4 as a patch against beta.5 (6 hunks); it applies to beta.6 unchanged. It is meant for Bondtech; the bridge's tests pass with and without it applied.

## Suggested Next Steps

1. Report findings 1 to 4 to Bondtech with the patch. The docs say the spool join is untested on a printer.
2. Never put `INDX_RUNOUT` in a sensor's `runout_gcode` on stock beta.5 or beta.6.
3. Use switch sensors, not motion sensors.
4. Before relying on a join in a long print, run `INDX_PRIME_TEST` on the partner.
