# INDX Bridge (unofficial): Changes

What changed in each version, newest first, and what to do after updating. The guide is `docs/indx-bridge.md`; "install step" numbers below refer to it. Until 0.3.0 the bridge was called indx-afc; the entries use the current names.

## 0.3.1

- **One spelling for the name in messages.** Console messages start with `INDX Bridge:` (0.3.0 printed `INDX-Bridge:`), the same as the name everywhere else. The name is used in three forms only: `INDX Bridge` in text and messages, `indx-bridge` for the folder, repository and zip, `indx_bridge` / `INDX_BRIDGE_*` in the configuration and commands.
- The module's class is `IndxBridge` (it was still `IndxAfc` inside). Nothing else changes.

## 0.3.0

- **Renamed from indx-afc to INDX Bridge (unofficial).** Every name changes, with no old command names kept; see *Moving from indx-afc* under Updating the Bridge in `docs/indx-bridge.md`. The installer removes the old links and renames `[indx_afc]` in your configuration; the bridge carries the page settings and a pushed lane over at its first start.
- No change in what the bridge does. (The older entries below use the new names.)

## 0.2.11

- **A lane pushed to its tool but not loaded is remembered across a restart** (`indx_bridge_rammed` in INDX's saved variables). After a failed load the next `INDX_BRIDGE_LOAD` skips the push and goes straight to INDX's feed with the lane following, as before, but now also after a Klipper restart. Without this, a restart in between made the bridge push again from the start, and AFC's push begins with a full-speed move that does not watch the buffer, into filament already at the tool. The mark clears when the tool loads, when the lane is unloaded or ejected, or when the lane no longer has filament.
- **Found on a printer: a 3 mm inside-diameter tube to the tool.** Over about 2 m the tip arrived off-center and caught at the tool's inlet; the buffer's advance switch closed there, so the push and the calibration both stopped short of the gears, and INDX reported `filament load was not detected ... (0.0 J load energy)`. A second `INDX_BRIDGE_LOAD` got it in. Use 2 mm inside-diameter tube from the buffer to the tool (Requirements).

## 0.2.10

- **A false warning with AFC DEV:** `set map: T0 in [AFC_stepper lane1] (now ('T0',))`. AFC DEV keeps a lane's `map` as a list, AFC main as text; the bridge's check now reads both.
- **The lane's motor is switched on whenever the bridge syncs the lane.** A synced lane whose motor is off follows nothing, and AFC switches a lane's motor off after many of its own moves.

## 0.2.9

- **The lane did not follow INDX's load feed.** Found on a printer: after the ram, lane 1 stood still while T0's extruder fed, so the filament never reached the melt zone and INDX stopped with `filament load was not detected after 124.9 mm (0.0 J load energy)`. AFC skips a sync when its own record says the lane is synced already, and something had unsynced the lane's stepper in Klipper without AFC knowing. The bridge now syncs the lane right before INDX's feed (`INDX_LOAD_FILAMENT`), checks in Klipper itself that the lane is on the extruder's motion queue, syncs it again if not, and stops before feeding if it still is not. Each bridge load now says `INDX Bridge: lane1 follows T0's load` just before the feed.
- The same check is used whenever the bridge keeps a lane synced.

## 0.2.8

- **AFC DEV's Eject failed with `takes 2 positional arguments but 3 were given`.** AFC DEV passes more arguments to `LANE_UNLOAD`, `TOOL_LOAD`, `TOOL_UNLOAD` and `CHANGE_TOOL` than AFC main did (`gcmd`, and for loads `load_to_gears` and `restore_temp`), some by position. The bridge's stand-ins for those four now take any extra arguments and pass them on to AFC where AFC's own code runs.
- Checked against AFC DEV 1e400e2 (November 2026): every name the bridge uses is there.

## 0.2.7

- **The cause of the first calibration failure was found.** It was not the bridge: AFC main calls Kalico's `manual_home` without the `gcmd` argument current Kalico requires, so every AFC homing move fails, AFC's own loads and unloads included. AFC DEV has the fix. Requirements and install step 3 now say to use AFC DEV, with the commands to switch.
- **The install check tests for it** and fails with "Update AFC to its DEV branch" when the installed AFC and Kalico do not match.

## 0.2.6

- **`INDX_BRIDGE_CALIBRATE` homes at AFC's calibration speed.** 0.2.5 homed toward the buffer at AFC's long-move speed, which AFC itself never does (its own calibration and ram home at the short-move speed). (The first failure on a printer turned out to have another cause: see 0.2.7.)
- **When AFC's homing move fails, the calibration stops** and says what to do, instead of starting a second homing move back to the load sensor, which failed the same way.

To update from 0.2.5: Updating the Bridge in the guide, then run the calibration again. To update from older: the same, plus the 0.2.5 notes.

## 0.2.5

- **`INDX_BRIDGE_CALIBRATE`** measures each lane's path to its tool's gears and saves `dist_hub`, so there is no tape measure step (install step 13). It works the way the ram does, against the buffer's advance switch, with the tool empty on the head and the lane unsynced, and leaves the filament back at the load sensor.
- **The guide was wrong about a long `dist_hub`.** It said a few cm too long did no harm. With AFC's default `load_then_home`, the ram moves `dist_hub` minus 20 mm without watching the buffer, so too long drives the filament into the gears at full speed. The calibration saves a value 35 mm under the measured path; set by hand, keep it 30 to 50 mm under.
- **The guide reviewed** for contradictions and repetition. Install steps are renumbered (14 steps: spools in at 12, calibration at 13), `dist_hub` is explained once under AFC Settings, the buffer fallback rules once under AFC Settings, and `[AFC_prep] enable: False` is consistently a setting to make. Updating the bridge has its own section.

## 0.2.4

Checked against INDX v1.0-beta.11. Every name, command, event and latch move the bridge uses is still there; the latch sync and the event delivery are the same code as beta.7's, the extension API is unchanged (level 1, `indx_extension.py` identical), and the three spool join bugs stay fixed. One fix:

- **The print's feedrate after a tool change.** Beta.11's `CHANGE_TOOL` and `T<n>` pass `KEEP_F=1`, so the change puts back the feedrate it was called at (a slicer may leave the `F` out of its next travel). The bridge sends `CHANGE_TOOL TOOL=<n>` and AFC's `T<n>` to INDX's change directly, so it now passes `KEEP_F=1` too. Without it the first move after each change could run at the change's speed. Older betas ignore it.

Checked and fine with the bridge:

- **INDX's status is built key by key** (beta.11). The bridge reads it with `.get()`, which builds the key.
- **What a running print cannot take.** Of the commands the bridge sends, only `INDX_UNLOAD` and `INDX_PRIME_TEST` are refused, and only while a print runs; the bridge unloads and prime-tests only with no print running or a paused one, and never sets a loaded tool's material to empty.
- **The latch at rest** (an empty head's latch closes when idle) and every other latch move run through `_TC_LATCH_MOVE` or `SG_HOME_E`, which unsync the lanes.
- **RESUME runs INDX's hook through Mainsail's and Fluidd's `_CLIENT_VARIABLE`.** The bridge's resume calls the same RESUME underneath, so the hook still runs.
- **The startup gate.** For about 5 s after a restart INDX refuses tool changes; an `INDX_BRIDGE_LOAD` then stops at the pickup, before any lane moves. Try again.

After updating INDX to beta.11, restart Moonraker and Klipper as Bondtech's notes say, then run `check_compat.py`.

## 0.2.3

Two safety checks taken from another AFC-on-INDX build (`afc-indx-beta-20261116-2`, see `afc-indx-build-review.md`):

1. **Nozzle rating on `M104`/`M109`.** INDX's tool change and load refuse a temperature above the nozzle's rating, but `INDX_SET_TEMP`, where `M104` and `M109` go, does not, so only the heater's `max_temp` stopped a slicer `M104 S320`. The bridge now refuses anything above `nozzle_max_temp` (from `printer.indx_api`, else INDX's constants, else 300 C). Outside a print: an error, nothing heated. In a print: nothing heated, a note on the INDX page, and the print pauses (`RESUME` or cancel after fixing the slicer).
2. **A lane that feeds a tool without a hotend** (`no_heat_tools`) stops Klipper's start with a config error naming the lane and the tool.

## 0.2.2

Checked against INDX v1.0-beta.7. Nothing broke: every name, command, event and latch move the bridge uses is unchanged, and the latch sync code is the same as beta.6's. What beta.7 changes, and what that means here:

- **Runouts under a tool manager.** Beta.7 leaves every runout to a registered tool manager: no pause and no spool join from INDX. The bridge registers as one (for the latch sync) but feeds the tools rather than owning them, and it already tells INDX's spool join to treat it that way. So INDX's spool join still decides, as before. The tests now check this against beta.7's real code; with that part of the bridge turned off, 24 checks fail.
- **The three spool join bugs are fixed in beta.7.** The bridge sees that and stands its workarounds aside (`INDX_RUNOUT`, `_INDX_REMAP_JOINED`, the runout during a pickup). On beta.5 and beta.6 they stay in place.
- **The retraction guard** (beta.7 holds back retractions right after a pickup while the latch is in mesh) does not touch the bridge: its unload pull runs through INDX's own `_TC_LATCH_MOVE`, the guard counts that as INDX's, and the lane ram moves only the lane.
- **Filament presets** are read from `printer.indx_api` (beta.7), with `LOAD_FILAMENT`'s variable as the fallback.
- `check_compat.py` notes beta.7's runout hand-off.

Do not list runout sensors under `[indx_remap] runout_sensors` with the bridge: it gives the lanes' sensors to INDX itself.

## 0.2.1

Checked against INDX v1.0-beta.6. The bridge's names, commands and events are all still there, every beta.6 latch move unsyncs the lanes, and the three spool join bugs are still in beta.6 (the workarounds stay, and the patch applies to beta.6 unchanged). Two fixes:

1. **The tool on the head changed by hand** (seat, release, forget; beta.6 puts these on the INDX page) left the old lane synced, or the new one unsynced, until the next tool change. The bridge now follows every change of INDX's tool on the head (its `tool_state` event).
2. **A stopped park** (beta.6 keeps the tool recorded on the head, not verified, when it may be in its dock) would have synced that tool's lane. It now stays unsynced until you say what is on the head.

Also: `check_compat.py` checks the latch sync in INDX's macros and the two names the fixes use; the settings card's place on beta.6's new page.

After updating INDX to beta.6, run its lock force step again as its notes say (`INDX_CAL_LOCK CONFIRM=1`, every tool unloaded: `INDX_BRIDGE_UNLOAD_ALL` first), then load again with `INDX_BRIDGE_LOAD_ALL`.

## 0.2.0

Found reviewing 0.1 against beta.5, current AFC and current Kalico:

1. **Unload pull** left 65 mm pushed into a stationary lane (the pull is a latch move). Now in parts with the lane following.
2. **AFC's PAUSE/RESUME wrappers** added a Z hop and restored a stale position and offsets around INDX's. Their moves are taken out.
3. **AFC took `M104`/`M109` back** when its start ran after the bridge's. Now re-checked for 12 s and on every `INDX_BRIDGE_SYNC`.
4. **AFC owns `CHANGE_TOOL`**, so INDX's `CHANGE_TOOL TOOL=n` failed. Now passed through.
5. **Start-up reconcile** ran outside Kalico's G-code lock, which Kalico refuses. Now inside it.
6. **INDX's own load buttons** fed air (no ram). `LOAD_FILAMENT` is wrapped.
7. **Spool join needed a file patch** to INDX. Now a runtime shim: nothing in the INDX checkout changes, updates stay clean.
8. **Three INDX beta.5 lock/logic bugs** are worked around (see `docs/spool-join-review.md`); `patches/indx-remap-beta5-fixes.patch` is the same as a patch, for Bondtech. You do not need to apply it.

New: settings card, paused-print reload, load/unload all, print start lane check, color sync, prime test option, `check_compat.py`, installer finds Kalico.
