# AFC-Bridge: a Box Turtle Feeding INDX Tools

A Klipper (Kalico) extension that lets an [AFC](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On) Box Turtle feed Bondtech's INDX toolchanger, one lane per tool. INDX keeps doing every tool change, load and unload; AFC drives the lanes, with a buffer per lane keeping each lane in step with its tool's extruder.

- Assisted loading and unloading from the Box Turtle, from INDX's buttons or AFC's
- `INDX_AFC_CALIBRATE`: measures each lane's path to its tool's gears and saves `dist_hub`
- Lanes stay still during INDX's latch moves; only the tool on the head has a synced lane
- INDX's spool join hears the Box Turtle's sensors
- An install check (`tools/check_compat.py`) to run after any INDX, AFC or Kalico update

**Status:** 0.2.11. Tested against stand-ins for Kalico, AFC and INDX, and against INDX's real `indx_remap.py` (beta.6, beta.7, beta.11). Early use on a printer; go through the test plan in the guide before a real print.

## Requirements

- INDX Suite on Kalico (INDX's full install), v1.0-beta.11 recommended
- AFC on its **DEV** branch (AFC main fails every homing move on current Kalico)
- One buffer per lane, each lane's tube going straight to its own tool (no hub)

## Install

On the printer:

```sh
git clone https://github.com/RyuNinja00/AFC-Bridge ~/indx-afc
bash ~/indx-afc/install.sh
```

Then follow **[the guide](docs/indx-afc.md)**: AFC settings, `[indx_afc]` in `printer.cfg`, calibration and the test plan. Install step 6 onward covers the bridge itself.

## Update

```sh
cd ~/indx-afc && git pull && bash install.sh
sudo systemctl restart klipper
```

Read the guide's What Changed entry for the new version first.

## Files

| Path | What it is |
| --- | --- |
| `klipper_extras/indx_afc.py` | The bridge (linked into Kalico's `klippy/extras` by the installer) |
| `macros/indx_afc.cfg` | Its macros and INDX settings card (linked into INDX's `ext` folder) |
| `install.sh` | Installs, re-installs (`bash install.sh`) or removes (`bash install.sh remove`) |
| `tools/check_compat.py` | Checks the installed INDX, AFC and Kalico for everything the bridge relies on |
| `tests/test_indx_afc.py` | Tests: `python3 tests/test_indx_afc.py [path/to/indx_remap.py]` |
| `docs/indx-afc.md` | The guide: design, install, settings, commands, test plan, changes |
| `docs/spool-join-review.md`, `patches/` | Notes and a patch on INDX beta.5/beta.6 spool join bugs, fixed in beta.7 |
