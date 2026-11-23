# INDX Bridge (unofficial): a Box Turtle Feeding INDX Tools

*Not made or supported by Bondtech or the AFC (Armored Turtle) project. Until 0.3.0 it was called indx-afc.*

Four Box Turtle lanes feed four INDX tools, one lane per tool. INDX does every tool change, load and unload; AFC drives the lanes. You keep AFC's assisted load and unload, a buffer per lane that keeps that lane in step with the extruder, AFC's lane LEDs and Spoolman tracking, and the Mainsail/Fluidd AFC panel.

Version 0.3.1, written against INDX v1.0-beta.11 (beta.7, beta.6 and beta.5 work too), AFC DEV (1e400e2, November 2026) and current Kalico.

**Early testing on a printer** (one Voron 2.4 with a 4-lane Box Turtle; fixes from it are in What Changed). The logic is tested against stand-ins for Kalico's G-code lock, AFC and INDX's macros, and against INDX's real `indx_remap.py` from beta.6, beta.7 and beta.11 (`tests/test_indx_bridge.py`: 136 checks, 157 with INDX's file). Go through the Test Plan before a real print.

## Contents

1. Design
2. Features
3. A Spool From Start to Finish
4. How It Works, in Detail
5. Requirements
6. Install, Step by Step
7. Updating the Bridge (and moving from indx-afc)
8. Removing the Bridge
9. AFC Settings
10. Bridge Settings
11. Commands
12. Test Plan
13. Known Limits
14. What Changed

## Design

**INDX stays in charge of the tools; AFC only feeds them.** INDX already does tool changes well: its pickup and park checks, its tool map and print start dialog, its heater model, its tuned load, prime and tip shaping, and its spool join. AFC is good at moving filament: assisted loading from the spool, a buffer that keeps a lane in step with the extruder, lane LEDs, Spoolman. The bridge joins the two and leaves each one doing its own job.

| Job | Done by |
| --- | --- |
| `T<n>`, tool changes, the tool map, `INDX_PRINT_START` | INDX |
| Heating (`M104`/`M109`, through INDX's heat rules) | INDX |
| Loading and unloading a tool (heat, feed, prime, tip shaping) | INDX, with the lane following |
| Getting filament from the spool to the tool's gears, and back | AFC's lane motor, started by the bridge |
| Keeping the lane in step with the extruder while printing | AFC's buffer, on the lane of the tool on the head |
| A spool running out | INDX's spool join, hearing AFC's lane sensors |
| Which lane moves when | The bridge |

**One lane per tool.** Lane 1 feeds T0, lane 2 feeds T1, and so on, each through its own tube and its own buffer, with no hub. A tool change never unloads anything: each tool keeps its filament in its gears, so a change is as fast as INDX on its own.

**A thin layer, not a fork.** The bridge is one Python module and one config file next to stock AFC and stock INDX. Nothing in either is edited, so both keep updating normally. The bridge adjusts a few things at start (listed under Known Limits), and `check_compat.py` tells you after an update if any of them moved.

**Registered with INDX as its tool manager, but not owning the tools.** INDX's extension API lets one module register as the tool manager. INDX then unsyncs every synced stepper around its latch moves, which is the only safe way to share the extruder motor with a lane. The bridge registers for that alone: it tells INDX it does not own `T<n>` or the tool changes, and keeps INDX's tool map and spool join on.

**Compared with the other AFC-on-INDX build** (`afc-indx-beta-20261116-2`, reviewed in `afc-indx-build-review.md`): that one makes AFC the owner of tool changes. It is the choice for more lanes than tools (several spools through a hub into one tool); this bridge is the closer fit for one lane per tool. INDX allows one tool manager, so only one of the two can be installed.

## Features

- **Lane calibration.** `INDX_BRIDGE_CALIBRATE` measures each lane's path to its tool's gears and saves it as `dist_hub`. No tape measure.
- **Assisted loading.** `INDX_BRIDGE_LOAD`, AFC's Load button, or INDX's own Load buttons: the lane drives the filament from the Box Turtle to the tool's gears, then INDX's load finishes it with the lane following. `INDX_BRIDGE_LOAD_ALL` loads every lane that has filament.
- **Assisted unloading.** `INDX_BRIDGE_UNLOAD` or INDX's Unload button: INDX shapes the tip, the pull out of the gears runs in short steps with the lane taking each one back, and the lane parks the filament at its load sensor. `INDX_BRIDGE_UNLOAD_ALL` unloads every tool.
- **A buffer per lane while printing.** Only the lane of the tool on the head is synced to the extruder; its buffer slows or speeds it to stay in step.
- **Safe latch moves.** No lane moves while INDX locks or unlocks a tool, or while a tool sits in its dock.
- **Spool join with the Box Turtle's sensors.** When a spool runs out, another loaded tool with the same material, nozzle and color takes over where the print stopped. Without one, the print pauses once.
- **Lane colors into INDX.** `INDX_BRIDGE_SYNC COLORS=1` copies AFC's lane colors (Spoolman's, for example) into INDX, which the spool join matches on.
- **A lane check at print start.** A tool the print uses whose spool is out, or an empty tool whose lane has filament, is reported (or stops the print start, if you choose).
- **Reloading during a pause.** With the print paused, unload and reload the tool on the head; the head goes back where the pause parked it and the nozzle back to the print's temperature.
- **Nozzle rating.** `M104`/`M109` above the INDX nozzle's rating are refused; in a print, the print pauses instead.
- **Follows hand changes.** Seating, releasing or forgetting a tool by hand moves the lane sync with it.
- **A settings card on the INDX page**, and `INDX_BRIDGE_STATUS` for the whole picture in one command.
- **An install check** (`check_compat.py`) to run after any INDX, AFC or Kalico update.

## A Spool From Start to Finish

Lane 1 and T0 as the example. Once, before the first load ever, the lane has been calibrated (install step 13).

1. **Insert the spool.** Feed the filament into lane 1 and tip the prep lever. AFC's lane motor grabs it and drives it to the lane's load sensor, inside the Box Turtle. With `hub: direct` it stops there: nothing goes down the tube yet. The lane shows Loaded. The bridge does nothing at this stage.
2. **Load T0.** `INDX_BRIDGE_LOAD LANE=lane1` (or any Load button). The bridge checks the lane has filament and T0 is empty, and takes the material and color from AFC. INDX picks up the empty T0. The lane motor pushes the filament through the buffer and the tube until it reaches T0's gears, stalls, and closes the buffer's advance switch. The lane is synced to the extruder, and INDX's load heats, feeds, primes and shapes the tip. T0 stays on the head, loaded.
3. **Tool changes.** At each lock and unlock, INDX unsyncs the lane. While T0 is parked its lane stays idle, its filament waiting in T0's gears. When T0 comes back to the head, lane 1 is synced again.
4. **Printing.** The buffer trims lane 1's speed: advance switch closed, the lane slows; trailing switch closed, it speeds up.
5. **The spool runs out.** The prep sensor opens as the tail passes. If another loaded tool has the same filament, INDX switches to it at the exact print point; otherwise the print pauses.
6. **Unload.** `INDX_BRIDGE_UNLOAD LANE=lane1`: INDX heats T0 and shapes the tip, the pull comes out of the gears in short steps with the lane following, and the lane pulls the filament back to its load sensor. AFC's Eject then takes it out of the lane.

## How It Works, in Detail

- **Lane sync.** Only the lane of the tool on the head is synced to the extruder, with its buffer trimming it. Every other lane is unsynced and idle. A parked tool must never get filament pushed into its gears: INDX stops on that as a misload.
- **Latch moves.** The INDX extruder motor also drives the latch, so a synced lane would push and pull filament at every lock and unlock. Because the bridge is INDX's tool manager, INDX unsyncs every synced lane around each latch move and syncs it back. This covers every latch move, including those added in beta.6 (the pickup's close out of the dock, the park's latch homing in the dock, the clamp check after lost unlock steps). If INDX does not have the bridge as its manager, the bridge never syncs a lane.
- **Calibration.** With the tool empty on the head and the lane unsynced, the lane feeds until the buffer's advance switch closes against the still gears, the same thing the ram does in a load. It does this fast, backs off 20 mm and comes in again slowly, then pulls the filament back to the load sensor and saves `dist_hub` (see "dist_hub" under AFC Settings).
- **Load.** INDX's `LOAD_FILAMENT` is wrapped, so every way of loading works: the INDX page's Load button, `INDX_LOAD`, `INDX_LOAD_ALL`, AFC's Load button, `INDX_BRIDGE_LOAD`. With the empty tool on the head, the lane rams its filament to the tool's gears (the lane's buffer is the sensor), is synced, and INDX's load heats, feeds, primes and shapes the tip with the lane following.
- **Unload.** INDX's unload pulls 65 mm out of the tool's gears as a latch move, and INDX unsyncs the lane for latch moves. Left alone, the tool would push 65 mm back into a lane that stands still. The bridge wraps that pull (`_INDX_UNLOAD_PULL`): it runs in parts (`unload_chunk`, 8 mm), the lane taking each part back, then the lane pulls the rest to its load sensor. This works from the INDX page's Unload button too.
- **The tool on the head changed by hand.** Seating a tool by hand, releasing it, or telling INDX the head is empty (`INDX_SEAT_TOOL_BY_HAND`, `INDX_RELEASE_TOOL_BY_HAND`, `INDX_FORGET_TOOL`, the INDX page's "Head: by hand") moves the sync to the right lane at once. After a stopped park ("T<n> may still be on the head") INDX keeps the tool recorded on the head but not verified: its lane stays unsynced until you have said what is on the head.
- **Tool changes.** INDX's, unchanged. AFC's `T<n>` and `CHANGE_TOOL LANE=` (the panel's tool buttons too) end in INDX's tool change, through INDX's tool map. INDX's own `CHANGE_TOOL TOOL=n` keeps working although AFC owns that command name.
- **PAUSE and RESUME.** About 2 s after start, AFC wraps both: a Z hop on pause, and on resume a restore of the position and G-code offsets it saved. On INDX those jobs belong to INDX (the pause park, the way back, per-tool offsets), and INDX's own internal pauses would leave AFC's saved values stale. The bridge keeps AFC's wrappers for their bookkeeping and takes their moves out.
- **Temperatures.** At its start AFC replaces `M104`/`M109` with versions that heat the shared extruder for any `T`. The bridge puts INDX's heat rules back (`INDX_SET_TEMP`) and re-checks after AFC's start. It also refuses a temperature above the INDX nozzle's rating (`nozzle_max_temp`, 300 C unless you changed it), which `INDX_SET_TEMP` does not check: outside a print that is an error and nothing heats; in a print nothing heats and the print pauses, since an error would end it.
- **Runout.** The lanes' prep sensors are handed to INDX's spool join. Another loaded tool with the same material, nozzle and color takes over where the print stopped; a parked tool's runout moves its next change to a partner; without a partner the print pauses once. AFC's own infinite spool is never used: it swaps lanes into one extruder, and here each lane feeds a different tool.

## Requirements

- **INDX Suite on Kalico** (INDX's `install.sh`, not `--macros-only`), beta.11 recommended; beta.7, beta.6 and beta.5 work.
- **AFC's DEV branch** on current Kalico. AFC main (5ea84ea, August 2026) does not pass the `gcmd` argument that Kalico's `manual_home` now requires, so every AFC homing move fails on current Kalico: loads, unloads, the ram and the calibration all stop with `Error occurred when trying to home to ...`. AFC DEV has the fix. The install check tests for this.
- **Hardware:** each lane's outlet runs straight to its own tool, with no hub merging lanes, and a buffer per lane between the Box Turtle and the tool. Use 2 mm inside-diameter tube from each buffer to its tool: in a wider tube (3 mm) the tip wanders off-center over a long run and can catch at the tool's inlet, which stops the push short of the gears. Any buffer AFC drives with two switches works (a TurtleNeck, or the N3MI-DG Lane-Buffer that mounts in the Box Turtle's rear skirt). Four buffers are eight switch inputs.
- **Your INDX working on its own first:** `INDX_SETUP` finished, docks measured, every tool changing correctly with nothing loaded.

## Install, Step by Step

About an hour the first time, most of it wiring. Do it with the printer idle.

**1. Back up your configuration.**

```sh
cp -a ~/printer_data/config ~/printer_data/config.before-indx-bridge
```

**2. Install and check INDX first.** The INDX Suite full install (see Requirements). Make sure INDX changes every tool correctly before going further.

**3. Install AFC** with its own installer if it is not there yet, on its DEV branch (see Requirements), and set up the Box Turtle as a unit. To move an installed AFC to DEV (its repository folder is usually `~/AFC-Klipper-Add-On`):

```sh
cd ~/AFC-Klipper-Add-On
git remote set-branches --add origin DEV && git fetch origin DEV
git checkout -B DEV origin/DEV
./update-afc.sh -b DEV
sudo systemctl restart klipper
```

**4. Wire and set up one buffer per lane.** Each buffer has two switches. In AFC's terms:

- **advance**: closes when the lane pushes filament faster than the extruder takes it (filament backing up against the tool's gears). Loads and calibration stop on this switch, so it must be the right one.
- **trailing**: closes when the extruder pulls faster than the lane feeds.

At rest the **advance switch must be open**: a buffer resting on it stops every load at once (AFC also refuses with "buffer is already extended"). Resting on the trailing switch is fine; spring-loaded buffers like the Lane-Buffer rest there. Both closed at once is a wiring fault (AFC warns about it at start). Wire them, then check each with `QUERY_BUFFER BUFFER=TN_1` while pushing and pulling the filament by hand.

How AFC uses the switches: it does not hold the slide in the middle. It runs the lane at 1.1x from the moment trailing closes until advance closes, then at 0.9x until trailing closes again, so in a print the slide travels from one switch to the other and back. The lane only moves while the extruder does, so this happens over tens of mm of extrusion, not seconds. `filament_error_sensitivity` (AFC's buffer fault detection) is off by default; leave it off for the first prints. On, it uses faster corrections and pauses with "CLOG DETECTED" or "AFC NOT FEEDING" if a switch stays closed for (11 - sensitivity) x 10 mm of extrusion, which a spring-loaded buffer can trip at high sensitivity right after each pickup.

**5. Set AFC up for the bridge.** Three files in `~/printer_data/config/AFC/`; AFC Settings below has the details and examples:

- `AFC_Turtle_1.cfg`: in each `[AFC_stepper laneN]` add `hub: direct`, `map: T<n>` and `buffer: TN_<n>`; add one `[AFC_buffer TN_<n>]` per lane. Leave `dist_hub` as the installer set it: step 13 measures it. Do not guess a value, and load no tool before step 13 (see "dist_hub" under AFC Settings for why).
- `AFC_Hardware.cfg`: `pin_tool_start: buffer` in `[AFC_extruder extruder]`.
- `AFC.cfg`: `tool_cut`, `park`, `poop`, `kick` and `wipe` to `False`; `enable: False` in `[AFC_prep]`.

**6. Get the bridge onto the printer**, into `~/indx-bridge`. Keep that folder: Klipper loads the bridge from it. Either way works:

- **From GitHub** (easiest to update later), on the printer:

  ```sh
  git clone https://github.com/RyuNinja00/indx-bridge ~/indx-bridge
  ```

- **From the zip:** put `indx-bridge.zip` in your home folder on the printer (for example `scp indx-bridge.zip pi@<printer>:~/` from your computer), then on the printer:

  ```sh
  cd ~ && unzip -o indx-bridge.zip
  ```

**7. Run the installer.**

```sh
bash ~/indx-bridge/install.sh
```

It finds Kalico the way INDX's installer does (the running service, then `~/klipper`, `~/kalico`), links `indx_bridge.py` next to INDX's modules and `indx_bridge.cfg` into `~/printer_data/config/indx/ext/`, then runs the install check. It stops with a message if INDX, INDX's extension folder or AFC is missing. If it cannot find Kalico: `KLIPPER_DIR=~/<kalico folder> bash ~/indx-bridge/install.sh`.

The check prints `ok` or `FAIL` per item. A FAIL on an INDX or AFC name means that version is not supported yet: stop here.

**8. Tell the bridge which lane feeds which tool.** Add to `printer.cfg`:

```ini
[indx_bridge]
lanes: lane1, lane2, lane3, lane4   # the lane feeding T0, T1, T2, T3 ("-" for a dock no lane feeds)
```

Optional in the same section: `ram_relief_mm` and `load_type`, the start values of the Bridge Settings `ram_relief` and `load_type`.

**9. Do not give INDX the lane sensors yourself.** Leave `[indx_remap] runout_sensors` empty: the bridge hands the lanes' sensors to INDX.

**10. Restart the Klipper service.** A plain `RESTART` does not load a new Python module:

```sh
sudo systemctl restart klipper
```

**11. Check it started.** Wait 15 s (AFC finishes its own start after about 2 s), then run `INDX_BRIDGE_STATUS` in the console. It should say `on`, list each lane against its tool, and show no warnings; each warning names the setting to fix. `INDX_TOOL_MANAGER` should answer `INDX > Tool manager: indx_bridge 0.3.1. ...`. INDX creates that command only while a tool manager is registered, so `Unknown command` means the bridge did not register (`INDX_STATUS` does not list the tool manager). On the INDX page, More, Settings, Extensions shows the "Box Turtle (AFC)" card.

**12. Insert the spools.** Each spool's filament into its lane until AFC drives it to the load sensor and the lane shows Loaded. Every tool stays empty.

**13. Calibrate the lanes.** Hotends can stay cold: nothing is extruded.

```
INDX_BRIDGE_CALIBRATE LANE=ALL
```

For each lane in turn: INDX picks up its empty tool, the lane feeds until its buffer's advance switch closes at the gears (fast, then back 20 mm and again slowly), and the lane pulls the filament back to its load sensor. At the end it prints, per lane, the length it measured and the `dist_hub` it saved to `AFC_Turtle_1.cfg`. The last tool calibrated stays on the head, empty. No restart needed.

Watch the first lane. It moves at AFC's calibration speed (the lane's short-move speed), so a path of about 2 m takes a minute or two. If the switch never closes within 3000 mm, it stops, brings the filament back and says so: check the path, or add `MAX=4000`. To do one lane: `INDX_BRIDGE_CALIBRATE LANE=lane1` (or `TOOL=0`). To see the number without saving it, add `SAVE=0`. Calibrate a lane again whenever you change its tube.

If AFC reports `Error occurred when trying to home to lane1_buffer`, AFC's homing move itself failed, before any measuring. The calibration stops there and leaves `dist_hub` alone; the filament may be partly down the tube, so bring it back with `LANE_UNLOAD LANE=lane1` or by hand. AFC writes the reason to `~/printer_data/logs/AFC.log`, after the line with `Homing start endstop=`. `manual_home() missing 1 required positional argument: 'gcmd'` there means AFC main on current Kalico: move AFC to DEV (install step 3).

**14. Work through the Test Plan below, in order,** before the first real print.

## Updating the Bridge

For a new version of the bridge. Your `printer.cfg`, your AFC files (calibrated `dist_hub` values included) and the bridge settings on the INDX page are all kept. Do it with the printer idle, nothing printing.

**1. Read the new version's What Changed entry** at the end of this guide. It says if anything must be done after the update.

**2. Get the new version and run the installer.** On the printer (SSH):

- **Installed from GitHub:**

  ```sh
  cd ~/indx-bridge && git pull && bash install.sh
  ```

- **Installed from the zip:** copy the new `indx-bridge.zip` to the printer's home folder (for example `scp indx-bridge.zip pi@<printer>:~/` from your computer), then:

  ```sh
  cd ~ && unzip -o indx-bridge.zip && bash ~/indx-bridge/install.sh
  ```

  `-o` replaces the old files without asking.

The installer ends with the install check: every line should say `ok`. A `FAIL` means this version does not match your INDX or AFC: do not print, and go back to the previous version (below).

**3. Restart the Klipper service.** A plain `RESTART` from the console does not load the new Python module:

```sh
sudo systemctl restart klipper
```

**4. Check the version.** Wait 15 s, then run `INDX_TOOL_MANAGER` in the console. It should name `indx_bridge` with the new version number. `INDX_BRIDGE_STATUS` should say `on` with no warnings.

**5. Do what What Changed asks**, if anything. From 0.2.4 or older: every tool empty, each lane Loaded, then `INDX_BRIDGE_CALIBRATE LANE=ALL` (install steps 12 and 13) before the next load.

**Going back to the previous version:** from GitHub, `cd ~/indx-bridge && git checkout <older commit>` then steps 2 to 5 without the `git pull`; from the zip, the same steps with the old zip.

### Moving from indx-afc (0.2.11 and older)

The bridge was called indx-afc until 0.3.0. Everything with that name is renamed: the folder (`~/indx-bridge`), the module (`indx_bridge.py`), the `printer.cfg` section (`[indx_bridge]`), the commands (`INDX_BRIDGE_*`), the settings macro (`_INDX_BRIDGE`), the tool manager name (`indx_bridge`) and the GitHub repository (`RyuNinja00/indx-bridge`). The old command names are gone.

On the printer (SSH), printer idle:

```sh
mv ~/indx-afc ~/indx-afc.old
git clone https://github.com/RyuNinja00/indx-bridge ~/indx-bridge
bash ~/indx-bridge/install.sh
sudo systemctl restart klipper
```

The installer:

- removes indx-afc's links (`indx_afc.py` and `indx/ext/indx_afc.cfg`), so the old version cannot load next to the new one;
- renames `[indx_afc]` to `[indx_bridge]` in your configuration files, keeping each changed file as `<file>.before-indx-bridge`;
- lists any of your own files that still use `INDX_AFC_*` commands (a print start macro, for example). Change those to `INDX_BRIDGE_*` by hand.

At the first start the bridge carries over what indx-afc saved: the settings from the INDX page (it says `INDX Bridge: N settings carried over from indx-afc`) and a lane left pushed to its tool by a failed load. Your AFC files and calibrated `dist_hub` values are not touched.

Check with `INDX_TOOL_MANAGER` (it names `indx_bridge 0.3.1`) and `INDX_BRIDGE_STATUS`. When everything works, `rm -rf ~/indx-afc.old`.

| Was (indx-afc) | Now (INDX Bridge) |
| --- | --- |
| `~/indx-afc` | `~/indx-bridge` |
| `[indx_afc]` | `[indx_bridge]` |
| `INDX_AFC_STATUS`, `_LOAD`, `_UNLOAD`, `_LOAD_ALL`, `_UNLOAD_ALL`, `_SYNC`, `_CALIBRATE` | `INDX_BRIDGE_STATUS`, `_LOAD`, `_UNLOAD`, `_LOAD_ALL`, `_UNLOAD_ALL`, `_SYNC`, `_CALIBRATE` |
| `INDX_SETTING MACRO=_INDX_AFC ...` | `INDX_SETTING MACRO=_INDX_BRIDGE ...` |
| Console messages `INDX-AFC: ...` | `INDX Bridge: ...` |
| github.com/RyuNinja00/AFC-Bridge | github.com/RyuNinja00/indx-bridge |

### After an INDX, AFC or Kalico Update

The bridge is not updated, but what it relies on may have changed. Run the install check before printing. It reads the installed source and reports any name the bridge relies on that is gone:

```sh
python3 ~/indx-bridge/tools/check_compat.py
```

A `FAIL` means wait for a bridge update (or go back to the INDX or AFC version you had) before printing.

## Removing the Bridge

`bash ~/indx-bridge/install.sh remove`, take `[indx_bridge]` out of `printer.cfg`, and restart the Klipper service.

## AFC Settings

AFC's installer puts its configuration in `~/printer_data/config/AFC/`, in three files:

| File | What you change there |
| --- | --- |
| `AFC_Turtle_1.cfg` (your Box Turtle; the name follows the unit) | Each `[AFC_stepper laneN]`: add `hub`, `map` and `buffer` (`dist_hub` is set by `INDX_BRIDGE_CALIBRATE`). Add the `[AFC_buffer]` sections. The `[AFC_hub]` section's pin, if you need it free |
| `AFC_Hardware.cfg` | `[AFC_extruder extruder]`: `pin_tool_start: buffer` |
| `AFC.cfg` | `tool_cut`, `park`, `poop`, `kick`, `wipe` to `False`; `[AFC_prep] enable: False` |

**`AFC_Turtle_1.cfg`.** The lane sections are already there, with the pins, `rotation_distance` and a `dist_hub` from the installer. Add three lines to each:

```ini
[AFC_stepper lane1]
# ...the installer's lines (unit, pins, rotation_distance, prep, load, dist_hub, ...) stay
hub: direct                       # add: its own path to its own tool (not direct_load)
map: T0                           # add: the T number of the tool it feeds (as in [indx_bridge] lanes)
buffer: TN_1                      # add: this lane's own buffer

[AFC_stepper lane2]
hub: direct
map: T1
buffer: TN_2                      # every lane has its own buffer: line
# ...lane3 (map: T2, buffer: TN_3) and lane4 (map: T3, buffer: TN_4) the same way
```

Then one buffer section per lane, anywhere in the same file:

```ini
[AFC_buffer TN_1]
advance_pin: ^Turtle_1:<pin>      # wherever this buffer's switches are wired
trailing_pin: ^Turtle_1:<pin>

[AFC_buffer TN_2]
advance_pin: ^<mcu>:<pin>
trailing_pin: ^<mcu>:<pin>
# ...TN_3 and TN_4
```

Also in this file:

- Every lane needs its own `buffer:` line and a matching `[AFC_buffer ...]` section with both pins, or Klipper will not start. With `pin_tool_start: buffer`, AFC looks for a lane's buffer in the lane's section, then in `[AFC_extruder extruder]`, then in the unit section; if it finds none, Klipper stops with `buffer set as pin_tool_start ... [AFC_buffer None] is not found`.
- `[AFC_BoxTurtle Turtle_1]` has `hub: Turtle_1`. Leave it: each lane's `hub: direct` overrides it. If it has a `buffer:` line, remove it, so no lane falls back to one shared buffer.
- `[AFC_hub Turtle_1]` stays (nothing uses it with every lane direct). Its `switch_pin` holds the board's `HUB` input. To use that input for a buffer switch, set `switch_pin: virtual`.

**`dist_hub`** is the lane's path from its load sensor to its tool's gears. Set it with `INDX_BRIDGE_CALIBRATE` (install step 13), not by hand and not with AFC's `CALIBRATE_AFC` (its direct-hub calibration expects a toolhead sensor). Why it matters: AFC's ram (`load_then_home`) first moves `dist_hub` minus `load_undershoot` (20 mm) at full speed **without watching the buffer**, then homes on the advance switch for up to `load_undershoot` + `homing_overshoot` (20 + 50 mm). So:

- **Too long is dangerous:** the full-speed part drives the filament into the tool's still gears.
- **Too short is safe but slow:** the load stops short and has to be repeated.

The calibration saves the measured length less 35 mm (`homing_overshoot` 50 less 15): the full-speed part ends well before the gears, and the homing still reaches 15 mm past where the switch closed. If you ever set it by hand, keep it 30 to 50 mm under the real path.

**`AFC_Hardware.cfg`.**

```ini
[AFC_extruder extruder]
pin_tool_start: buffer            # change: INDX tools have no toolhead sensor; the load rams to the gears and stops on the buffer
# tool_stn, tool_stn_unload and the other lines: leave them, the bridge does not use them
```

**`AFC.cfg`.**

- In `[AFC]`: set `tool_cut`, `park`, `poop`, `kick` and `wipe` to `False` (leave `form_tip: False`). With the bridge, INDX runs the tool changes, purge and wipe, so AFC's versions never run; INDX tools have no cutter. Left on, AFC logs at every start that their positions in `AFC_Macro_Vars.cfg` are not set ("park is set to True and variable_park_loc_xy has not been updated"), and AFC's own `TOOL_LOAD` and `TOOL_UNLOAD` refuse to run.
- In `[AFC_prep]` (near the end): `enable: False`. Enabled, AFC moves every lane 5 mm out and back at each Klipper start, including lanes whose filament sits in a parked tool.
- `homing_enabled` and `home_to_tool` are not in the file: both default to True, which the ram and the calibration need. Only if you added either one with `False`, take it out.
- `unload_on_runout` is in the file commented out (`#unload_on_runout: True`) and defaults to False. Leave it commented out.

And in the console: no runout lanes (`SET_RUNOUT`) and no AFC lane remapping (`SET_MAP`).

The bridge checks all of these at start and lists what is off in `INDX_BRIDGE_STATUS`.

## Bridge Settings

On the INDX page under More, Settings, Extensions, "Box Turtle (AFC)" (beta.5: Settings, Extensions), or `INDX_SETTING MACRO=_INDX_BRIDGE VAR=<name> VALUE=<value>`. INDX keeps them across restarts.

| Setting | Default | What it does |
|---|---|---|
| `spool_join` | on | INDX's spool join and tool mapping stay on with the bridge. Off: a runout pauses |
| `print_check` | warn | At `INDX_PRINT_START`: a used tool whose spool is out, or an empty tool whose lane has filament. `off`, `warn` or `stop` |
| `load_type` | none | Material for a load when the spool has none in AFC |
| `prime_test` | off | Run INDX's `INDX_PRIME_TEST` after `INDX_BRIDGE_LOAD` |
| `ram_relief` | 0 mm | The lane backs off after the ram. Raise it if INDX reports a misload at the next pickup |
| `unload_chunk` | 8 mm | Length of each part of the unload pull. Keep it under your buffer's travel |

## Commands

| Command | What it does |
|---|---|
| `INDX_BRIDGE_STATUS` | On/off, tool on the head, AFC's loaded lane, spool join state, each lane, warnings |
| `INDX_BRIDGE_CALIBRATE LANE=<lane>\|ALL` (or `TOOL=<n>`) `[MAX=3000] [SAVE=0]` | Measure the lane's path to its tool's gears and save `dist_hub` (install step 13). The tool must be empty and the lane Loaded; not during a print. `MAX`: the longest path it tries, in mm. `SAVE=0`: report only. `ALL` skips tools with filament and lanes without |
| `INDX_BRIDGE_LOAD LANE=<lane>` (or `TOOL=<n>`) `[TYPE=] [COLOR=]` | Load with AFC's material and color for the lane |
| `INDX_BRIDGE_UNLOAD LANE=<lane>` (or `TOOL=<n>`) | `INDX_UNLOAD`, then the lane back to its load sensor |
| `INDX_BRIDGE_LOAD_ALL [TYPE=]` | Load every lane that has filament into its empty tool, the reference tool last |
| `INDX_BRIDGE_UNLOAD_ALL` | Unload every loaded tool a lane feeds |
| `INDX_BRIDGE_SYNC [COLORS=1]` | Match AFC to the tool on the head (no moves) and re-check the bridge's commands. `COLORS=1` copies AFC's lane colors into INDX, which the spool join matches on |
| `LOAD_FILAMENT ... [RAM=0\|1]` | INDX's own; `RAM=0` skips the lane's ram, `RAM=1` forces it |

Loads, unloads and calibration are refused while printing. **In a paused print** (a runout with no partner, for example), `INDX_BRIDGE_UNLOAD` and `INDX_BRIDGE_LOAD` work for the tool on the head: the load skips tip shaping, the head goes back to where the pause parked it, and the nozzle is set back to the print's temperature. Then `RESUME`.

AFC's Eject (`LANE_UNLOAD`) is refused while the tool still holds that lane's filament.

## Test Plan

After install step 13, in this order, nothing printing, a hand near the emergency stop.

1. **Start.** `INDX_BRIDGE_STATUS`: on, no warnings, no "not the bridge's any more" line. `INDX_TOOL_MANAGER` names `indx_bridge`.
2. **Empty tools.** Change tools a few times with nothing loaded: nothing is ever synced.
3. **One load.** `INDX_BRIDGE_LOAD LANE=lane1 TYPE=PLA`. The ram should stop at the gears with the buffer compressed. If the next pickup of T0 reports a misload, set `ram_relief` to 1 and repeat.
4. **Latch moves.** With T0 loaded, park it and pick it up. The lane motor must not turn during the latch moves.
5. **Extrude.** T0 hot on the head: extrude 200 mm slowly, then fast. The slide should leave the trailing switch, reach advance, and come back, without the filament grinding or buckling. If it never leaves trailing, the spring is stronger than the filament's push through the tube.
6. **Unload.** `INDX_BRIDGE_UNLOAD LANE=lane1`. Watch the buffer during the pull: it should work back and forth within its travel, 9 times with the default `unload_chunk`. If it bottoms out, lower `unload_chunk`. The filament ends at the lane's load sensor.
7. **INDX page.** Load and unload T0 from the INDX page's buttons: same behavior.
8. **By hand.** T0 loaded on the head: `INDX_BRIDGE_STATUS` shows lane1 SYNCED. Hold T0 and run `INDX_RELEASE_TOOL_BY_HAND`: nothing synced. Put T0 back on the head and run `INDX_SEAT_TOOL_BY_HAND TOOL=0`: lane1 SYNCED again.
9. **Pause and resume**, in a small print: `PAUSE`, check the head parks where INDX parks it with no extra Z hop, `RESUME`, check the print goes on at the right place and height. Repeat after a tool change, so the tool on the head differs from the first one.
10. **A two-tool print.**
11. **Runout:** two tools with the same material and color (`INDX_BRIDGE_SYNC COLORS=1`), a print using one; cut the filament before its lane's prep sensor. INDX should switch to the other tool where the print stopped.
12. **Runout with no partner:** the print pauses. `INDX_BRIDGE_UNLOAD`, new spool, `INDX_BRIDGE_LOAD`, `RESUME`.

## Known Limits

- The parts most likely to need tuning on a real printer: the ram and calibration against your buffer (its switch points, `ram_relief`), `unload_chunk` against your buffer's travel, and the first pause and resume.
- `INDX_COLD_PULL` is not lane-aware: its pulls are latch moves with the lane standing still. Unload the lane's filament first, or do cold pulls with the PTFE off the tool.
- The bridge replaces methods on live AFC and INDX objects at start (AFC's `CHANGE_TOOL`, `TOOL_LOAD`, `TOOL_UNLOAD`, `LANE_UNLOAD`, PAUSE/RESUME handlers, lane runout handlers; INDX's `_manager` and `_watch` in `indx_remap`) and wraps G-code commands (`CHANGE_TOOL`, `LOAD_FILAMENT`, `INDX_LOAD_FILAMENT`, `_INDX_UNLOAD_PULL`, `M104`/`M109`; on beta.5 and beta.6 also `INDX_RUNOUT` and `_INDX_REMAP_JOINED`). An update that renames them is caught by `check_compat.py`, by a start error, or by a line in `INDX_BRIDGE_STATUS`.
- On mainline Klipper (no `filament:runout` events) INDX only hears the runout of the lane in use, through AFC. INDX needs Kalico anyway.

## What Changed in 0.3.1

- **One spelling for the name in messages.** Console messages start with `INDX Bridge:` (0.3.0 printed `INDX-Bridge:`), the same as the name everywhere else. The name is used in three forms only: `INDX Bridge` in text and messages, `indx-bridge` for the folder, repository and zip, `indx_bridge` / `INDX_BRIDGE_*` in the configuration and commands.
- The module's class is `IndxBridge` (it was still `IndxAfc` inside). Nothing else changes.

## What Changed in 0.3.0

- **Renamed from indx-afc to INDX Bridge (unofficial).** Every name changes, with no old command names kept; see *Moving from indx-afc* under Updating the Bridge. The installer removes the old links and renames `[indx_afc]` in your configuration; the bridge carries the page settings and a pushed lane over at its first start.
- No change in what the bridge does. (The older entries below use the new names.)

## What Changed in 0.2.11

- **A lane pushed to its tool but not loaded is remembered across a restart** (`indx_bridge_rammed` in INDX's saved variables). After a failed load the next `INDX_BRIDGE_LOAD` skips the push and goes straight to INDX's feed with the lane following, as before, but now also after a Klipper restart. Without this, a restart in between made the bridge push again from the start, and AFC's push begins with a full-speed move that does not watch the buffer, into filament already at the tool. The mark clears when the tool loads, when the lane is unloaded or ejected, or when the lane no longer has filament.
- **Found on a printer: a 3 mm inside-diameter tube to the tool.** Over about 2 m the tip arrived off-center and caught at the tool's inlet; the buffer's advance switch closed there, so the push and the calibration both stopped short of the gears, and INDX reported `filament load was not detected ... (0.0 J load energy)`. A second `INDX_BRIDGE_LOAD` got it in. Use 2 mm inside-diameter tube from the buffer to the tool (Requirements).

## What Changed in 0.2.10

- **A false warning with AFC DEV:** `set map: T0 in [AFC_stepper lane1] (now ('T0',))`. AFC DEV keeps a lane's `map` as a list, AFC main as text; the bridge's check now reads both.
- **The lane's motor is switched on whenever the bridge syncs the lane.** A synced lane whose motor is off follows nothing, and AFC switches a lane's motor off after many of its own moves.

## What Changed in 0.2.9

- **The lane did not follow INDX's load feed.** Found on a printer: after the ram, lane 1 stood still while T0's extruder fed, so the filament never reached the melt zone and INDX stopped with `filament load was not detected after 124.9 mm (0.0 J load energy)`. AFC skips a sync when its own record says the lane is synced already, and something had unsynced the lane's stepper in Klipper without AFC knowing. The bridge now syncs the lane right before INDX's feed (`INDX_LOAD_FILAMENT`), checks in Klipper itself that the lane is on the extruder's motion queue, syncs it again if not, and stops before feeding if it still is not. Each bridge load now says `INDX Bridge: lane1 follows T0's load` just before the feed.
- The same check is used whenever the bridge keeps a lane synced.

## What Changed in 0.2.8

- **AFC DEV's Eject failed with `takes 2 positional arguments but 3 were given`.** AFC DEV passes more arguments to `LANE_UNLOAD`, `TOOL_LOAD`, `TOOL_UNLOAD` and `CHANGE_TOOL` than AFC main did (`gcmd`, and for loads `load_to_gears` and `restore_temp`), some by position. The bridge's stand-ins for those four now take any extra arguments and pass them on to AFC where AFC's own code runs.
- Checked against AFC DEV 1e400e2 (November 2026): every name the bridge uses is there.

## What Changed in 0.2.7

- **The cause of the first calibration failure was found.** It was not the bridge: AFC main calls Kalico's `manual_home` without the `gcmd` argument current Kalico requires, so every AFC homing move fails, AFC's own loads and unloads included. AFC DEV has the fix. Requirements and install step 3 now say to use AFC DEV, with the commands to switch.
- **The install check tests for it** and fails with "Update AFC to its DEV branch" when the installed AFC and Kalico do not match.

## What Changed in 0.2.6

- **`INDX_BRIDGE_CALIBRATE` homes at AFC's calibration speed.** 0.2.5 homed toward the buffer at AFC's long-move speed, which AFC itself never does (its own calibration and ram home at the short-move speed). (The first failure on a printer turned out to have another cause: see 0.2.7.)
- **When AFC's homing move fails, the calibration stops** and says what to do, instead of starting a second homing move back to the load sensor, which failed the same way.

To update from 0.2.5: Updating the Bridge, then run the calibration again. To update from older: the same, plus the 0.2.5 notes.

## What Changed in 0.2.5

- **`INDX_BRIDGE_CALIBRATE`** measures each lane's path to its tool's gears and saves `dist_hub`, so there is no tape measure step (install step 13). It works the way the ram does, against the buffer's advance switch, with the tool empty on the head and the lane unsynced, and leaves the filament back at the load sensor.
- **The guide was wrong about a long `dist_hub`.** It said a few cm too long did no harm. With AFC's default `load_then_home`, the ram moves `dist_hub` minus 20 mm without watching the buffer, so too long drives the filament into the gears at full speed. The calibration saves a value 35 mm under the measured path; set by hand, keep it 30 to 50 mm under.
- **The guide reviewed** for contradictions and repetition. Install steps are renumbered (14 steps: spools in at 12, calibration at 13), `dist_hub` is explained once under AFC Settings, the buffer fallback rules once under AFC Settings, and `[AFC_prep] enable: False` is consistently a setting to make. Updating the bridge has its own section.

## What Changed in 0.2.4

Checked against INDX v1.0-beta.11. Every name, command, event and latch move the bridge uses is still there; the latch sync and the event delivery are the same code as beta.7's, the extension API is unchanged (level 1, `indx_extension.py` identical), and the three spool join bugs stay fixed. One fix:

- **The print's feedrate after a tool change.** Beta.11's `CHANGE_TOOL` and `T<n>` pass `KEEP_F=1`, so the change puts back the feedrate it was called at (a slicer may leave the `F` out of its next travel). The bridge sends `CHANGE_TOOL TOOL=<n>` and AFC's `T<n>` to INDX's change directly, so it now passes `KEEP_F=1` too. Without it the first move after each change could run at the change's speed. Older betas ignore it.

Checked and fine with the bridge:

- **INDX's status is built key by key** (beta.11). The bridge reads it with `.get()`, which builds the key.
- **What a running print cannot take.** Of the commands the bridge sends, only `INDX_UNLOAD` and `INDX_PRIME_TEST` are refused, and only while a print runs; the bridge unloads and prime-tests only with no print running or a paused one, and never sets a loaded tool's material to empty.
- **The latch at rest** (an empty head's latch closes when idle) and every other latch move run through `_TC_LATCH_MOVE` or `SG_HOME_E`, which unsync the lanes.
- **RESUME runs INDX's hook through Mainsail's and Fluidd's `_CLIENT_VARIABLE`.** The bridge's resume calls the same RESUME underneath, so the hook still runs.
- **The startup gate.** For about 5 s after a restart INDX refuses tool changes; an `INDX_BRIDGE_LOAD` then stops at the pickup, before any lane moves. Try again.

After updating INDX to beta.11, restart Moonraker and Klipper as Bondtech's notes say, then run `check_compat.py`.

## What Changed in 0.2.3

Two safety checks taken from another AFC-on-INDX build (`afc-indx-beta-20261116-2`, see `afc-indx-build-review.md`):

1. **Nozzle rating on `M104`/`M109`.** INDX's tool change and load refuse a temperature above the nozzle's rating, but `INDX_SET_TEMP`, where `M104` and `M109` go, does not, so only the heater's `max_temp` stopped a slicer `M104 S320`. The bridge now refuses anything above `nozzle_max_temp` (from `printer.indx_api`, else INDX's constants, else 300 C). Outside a print: an error, nothing heated. In a print: nothing heated, a note on the INDX page, and the print pauses (`RESUME` or cancel after fixing the slicer).
2. **A lane that feeds a tool without a hotend** (`no_heat_tools`) stops Klipper's start with a config error naming the lane and the tool.

## What Changed in 0.2.2

Checked against INDX v1.0-beta.7. Nothing broke: every name, command, event and latch move the bridge uses is unchanged, and the latch sync code is the same as beta.6's. What beta.7 changes, and what that means here:

- **Runouts under a tool manager.** Beta.7 leaves every runout to a registered tool manager: no pause and no spool join from INDX. The bridge registers as one (for the latch sync) but feeds the tools rather than owning them, and it already tells INDX's spool join to treat it that way. So INDX's spool join still decides, as before. The tests now check this against beta.7's real code; with that part of the bridge turned off, 24 checks fail.
- **The three spool join bugs are fixed in beta.7.** The bridge sees that and stands its workarounds aside (`INDX_RUNOUT`, `_INDX_REMAP_JOINED`, the runout during a pickup). On beta.5 and beta.6 they stay in place.
- **The retraction guard** (beta.7 holds back retractions right after a pickup while the latch is in mesh) does not touch the bridge: its unload pull runs through INDX's own `_TC_LATCH_MOVE`, the guard counts that as INDX's, and the lane ram moves only the lane.
- **Filament presets** are read from `printer.indx_api` (beta.7), with `LOAD_FILAMENT`'s variable as the fallback.
- `check_compat.py` notes beta.7's runout hand-off.

Do not list runout sensors under `[indx_remap] runout_sensors` with the bridge: it gives the lanes' sensors to INDX itself.

## What Changed in 0.2.1

Checked against INDX v1.0-beta.6. The bridge's names, commands and events are all still there, every beta.6 latch move unsyncs the lanes, and the three spool join bugs are still in beta.6 (the workarounds stay, and the patch applies to beta.6 unchanged). Two fixes:

1. **The tool on the head changed by hand** (seat, release, forget; beta.6 puts these on the INDX page) left the old lane synced, or the new one unsynced, until the next tool change. The bridge now follows every change of INDX's tool on the head (its `tool_state` event).
2. **A stopped park** (beta.6 keeps the tool recorded on the head, not verified, when it may be in its dock) would have synced that tool's lane. It now stays unsynced until you say what is on the head.

Also: `check_compat.py` checks the latch sync in INDX's macros and the two names the fixes use; the settings card's place on beta.6's new page.

After updating INDX to beta.6, run its lock force step again as its notes say (`INDX_CAL_LOCK CONFIRM=1`, every tool unloaded: `INDX_BRIDGE_UNLOAD_ALL` first), then load again with `INDX_BRIDGE_LOAD_ALL`.

## What Changed in 0.2.0

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
