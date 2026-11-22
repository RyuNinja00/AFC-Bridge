#!/usr/bin/env python3
# INDX-AFC bridge: does this printer's INDX, AFC and Kalico still have the
# names the bridge uses? Reads source files only; changes nothing.
#   python3 tools/check_compat.py [--klipper ~/klipper] [--config ~/printer_data/config]
# Run it after every INDX, AFC or Kalico update, before printing.
import argparse
import glob
import os
import re
import sys

p = argparse.ArgumentParser()
p.add_argument("--klipper", default=os.path.expanduser("~/klipper"))
p.add_argument("--config", default=os.path.expanduser("~/printer_data/config"))
a = p.parse_args()
bad = warn = 0


def find(name):
    for sub in ("plugins", "extras"):
        f = os.path.join(a.klipper, "klippy", sub, name)
        if os.path.exists(f):
            return f
    return None


def read(f):
    try:
        with open(f, errors="replace") as h:
            return h.read()
    except OSError:
        return ""


def check(title, text, needles, fatal=True):
    global bad, warn
    missing = [n for n in needles if not re.search(n, text)]
    if not missing:
        print("  ok    %s" % title)
        return
    if fatal:
        bad += 1
    else:
        warn += 1
    print("  %s %s: not found: %s" % ("FAIL " if fatal else "warn ", title, ", ".join(missing)))


def src(name, fatal=True):
    global bad
    f = find(name)
    if f is None:
        if fatal:
            bad += 1
            print("  FAIL  %s is not installed in %s" % (name, a.klipper))
        return ""
    return read(f)


print("INDX-AFC bridge check (%s)" % a.klipper)
t = src("indx_api.py")
if t:
    check("INDX tool manager and latch sync (indx_api.py)", t,
          [r"self\.manager\b", r"INDX_CHANGE_TOOL", r"def latch_release", r"def latch_restore", r"def hooked", r"M99104"])
    check("INDX tool on the head events and suspect state (indx_api.py)", t, [r'"tool_state"', r'"attention"'], fatal=False)
t = src("indx_extension.py")
if t:
    check("INDX extension settings (indx_extension.py)", t, [r"settings_macro", r"owns_temp_commands", r"owns_tool_commands"])
t = src("indx_remap.py")
if t:
    check("INDX spool join (indx_remap.py)", t,
          [r"def _manager\(self\)", r"def _runout\(self, d", r"def _start_join\(self, d, to\)", r"def slicer_to_dock",
           r"self\.sensors\b", r"self\.dock_of\b", r"self\.sensor_names\b", r"def cmd_JOINED", r"filament:runout"])
    check("INDX spool join, beta.5 queue (indx_remap.py)", t, [r"join_busy", r"join_next", r"def _watch"], fatal=False)
    if re.search(r"def _manager_owns_tools", t):
        print("  note  indx_remap.py has the feeder-manager change itself (patched or fixed upstream): the bridge's shim is idle")
    if re.search(r"Left to the tool manager", t):
        print("  note  INDX leaves runouts to a registered tool manager (beta.7+): the bridge keeps INDX's spool join deciding through _manager()")
t = src("AFC.py")
if t:
    check("AFC (AFC.py)", t, [r"def CHANGE_TOOL\(self, cur_lane", r"def TOOL_LOAD\(self, cur_lane", r"def TOOL_UNLOAD\(self, cur_lane",
                              r"def LANE_UNLOAD\(self, cur_lane", r"homing_enabled", r"def save_vars"])
t = src("AFC_lane.py")
if t:
    check("AFC lanes (AFC_lane.py)", t,
          [r"class SpeedMode", r"class AssistActive", r"class AFCLaneState", r"class AFCMoveWarning", r"def sync_to_extruder",
           r"def unsync_to_extruder", r"def set_tool_loaded", r"def set_tool_unloaded", r"def move_to\(", r"def move_advanced\(",
           r"def _perform_pause_runout", r"def _perform_infinite_runout", r"def get_toolhead_pre_sensor_state", r"def do_enable",
           r"fila_prep", r"dist_hub", r"load_es", r"homing_overshoot", r"def get_toolhead_endstop"])
# Kalico (PR #1052) made gcmd a required argument of manual_home. An AFC
# that does not pass it fails every homing move: loads, the ram, unloads,
# INDX_AFC_CALIBRATE ("Error occurred when trying to home to ...").
hm = read(os.path.join(a.klipper, "klippy", "extras", "homing.py"))
st = src("AFC_stepper.py", fatal=False)
if st:
    m = re.search(r"def manual_home\(([^)]*)\)", hm)
    if m and re.search(r"\bgcmd\b", m.group(1)) and "=" not in m.group(1).split("gcmd")[1].split(",")[0]:
        if "manual_home_requires_gcmd" in st or re.search(r"manual_home\([^)]*gcmd", st):
            print("  ok    AFC passes gcmd to Kalico's manual_home")
        else:
            bad += 1
            print("  FAIL  AFC's homing moves call manual_home without gcmd, which this Kalico requires: every AFC homing move "
                  "fails (\"Error occurred when trying to home to ...\"). Update AFC to its DEV branch, which has the fix")
t = src("AFC_error.py")
if t:
    check("AFC pause and resume (AFC_error.py)", t, [r"def cmd_AFC_PAUSE", r"def cmd_AFC_RESUME", r"AFC_RENAME_PAUSE_NAME",
                                                     r"AFC_RENAME_RESUME_NAME", r"def reset_failure"])
t = src("AFC_functions.py")
if t:
    check("AFC functions (AFC_functions.py)", t, [r"def get_current_lane_obj", r"def handle_activate_extruder", r"def ConfigRewrite\(self, "])
t = "".join(read(f) for f in glob.glob(os.path.join(a.klipper, "klippy", "extras", "AFC_*.py")) + glob.glob(os.path.join(a.klipper, "klippy", "plugins", "AFC_*.py")))
if t:
    check("AFC units (load_then_home)", t, [r"def load_then_home\(self, "])
t = read(os.path.join(a.klipper, "klippy", "extras", "filament_switch_sensor.py"))
check("Klipper sends filament:runout (Kalico does; INDX's spool join hears the lanes' sensors with it)", t, [r"filament:runout"], fatal=False)
t = read(os.path.join(a.klipper, "klippy", "gcode.py"))
check("Klipper gcode (gcode.py)", t, [r"def run_script_from_command", r"ready_gcode_handlers"])

macros = "".join(read(f) for f in glob.glob(os.path.join(a.config, "indx", "*.cfg")))
if not macros:
    warn += 1
    print("  warn  no INDX macros in %s/indx: not checked" % a.config)
else:
    check("INDX macros", macros,
          [r"\[gcode_macro LOAD_FILAMENT\]", r"\[gcode_macro _INDX_UNLOAD_PULL\]", r"\[gcode_macro _TC_LATCH_MOVE\]",
           r"\[gcode_macro INDX_UNLOAD\]", r"\[gcode_macro _INDX_CHANGE_TOOL\]",
           r"\[gcode_macro INDX_SET_TEMP\]", r"\[gcode_macro INDX_SET_MATERIAL\]", r"variable_unload_length", r"filament_presets"])
    check("INDX beta.5+ extras (prime test, notes)", macros, [r"\[gcode_macro INDX_PRIME_TEST\]", r"\[gcode_macro _INDX_NOTE\]"], fatal=False)
    if "KEEP_F" in macros:
        print("  note  INDX's tool change puts the print's feedrate back (KEEP_F, beta.11+): the bridge passes it on")
    check("INDX latch moves that unsync the lanes (_TC_LATCH_MOVE, SG_HOME_E)", macros,
          [r"_INDX_LATCH_SYNC MODE=release", r"_INDX_LATCH_SYNC MODE=restore"])
    m = re.search(r"\[gcode_macro _INDX_UNLOAD_PULL\](.*?)\n\[", macros, re.S)
    if m and "_TC_LATCH_MOVE" not in m.group(1):
        warn += 1
        print("  warn  _INDX_UNLOAD_PULL no longer uses _TC_LATCH_MOVE: read it, the bridge replaces it for lane-fed tools")
if not os.path.exists(os.path.join(a.config, "indx", "ext", "indx_afc.cfg")):
    warn += 1
    print("  warn  %s/indx/ext/indx_afc.cfg is missing: run install.sh" % a.config)

print("%s (%d failed, %d warnings)" % ("Do NOT print with the bridge until the failed lines are sorted: update the bridge" if bad else
                                       "The names the bridge uses are all there" if not warn else "Usable; read the warnings", bad, warn))
sys.exit(1 if bad else 0)
