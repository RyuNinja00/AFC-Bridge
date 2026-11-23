# INDX <-> AFC bridge: a Box Turtle (or any AFC unit) feeding INDX tools,
# one lane per tool.
#
# This file may be distributed under the terms of the GNU GPLv3 license,
# like Klipper, Kalico, AFC and INDX.
#
# Written against INDX v1.0-beta.11 (beta.7, beta.6 and beta.5 work too), AFC main 5ea84ea /
# DEV 7324836 and Kalico 991e182 (2026-11-08). tools/check_compat.py checks an install.
#
# INDX does every tool change, load and unload. AFC drives the lanes:
#
#   lane sync     Only the lane of the tool on the head is synced to the
#                 extruder, with its own buffer trimming it. Every other lane
#                 is unsynced and idle, so a parked tool never gets filament
#                 pushed into its gears (INDX would stop on that as a misload).
#   latch moves   The INDX extruder motor also drives the latch. This module
#                 registers with INDX as its tool manager ([indx_extension
#                 indx_afc], macros/indx_afc.cfg), and INDX then unsyncs every
#                 synced lane around each latch move and syncs it back.
#   load          LOAD_FILAMENT (INDX's own: the INDX page's Load, INDX_LOAD,
#                 INDX_LOAD_ALL, the setup's filament step) is wrapped: with
#                 the empty tool on the head, the lane first rams its filament
#                 to the tool's gears (its buffer is the sensor, AFC's
#                 pin_tool_start: buffer), is synced, and INDX's load runs.
#                 INDX_AFC_LOAD does it in one command with AFC's material
#                 and color for the lane.
#   unload        INDX's unload pulls 65 mm out of the tool's gears as a latch
#                 move, during which INDX unsyncs the lane: the filament would
#                 be pushed back into a lane that stands still. _INDX_UNLOAD_PULL
#                 is wrapped: the pull runs in parts, the lane taking each part
#                 back, then the lane pulls the rest to its load sensor.
#   tool changes  INDX's. AFC's CHANGE_TOOL / T<n>, TOOL_LOAD, TOOL_UNLOAD and
#                 LANE_UNLOAD (the Mainsail/Fluidd AFC panel too) are
#                 redirected, so AFC's single-extruder MMU sequences never run.
#   PAUSE/RESUME  AFC wraps both at its PREP with a Z hop and a position
#                 restore of its own. On INDX the pause park, the way back and
#                 the G-code offsets (per tool) are INDX's: the bridge leaves
#                 AFC's wrappers their bookkeeping and takes their moves out.
#   M104/M109     INDX's heat rules (INDX_SET_TEMP). AFC's own would heat the
#                 shared extruder for any T.
#   runout        The lanes' prep sensors are given to INDX's spool join
#                 ([indx_remap]), which INDX turns off for every tool manager:
#                 the bridge tells it this manager only feeds the tools. A
#                 partner tool takes over, or the print pauses. AFC's own
#                 infinite spool is never used (it swaps lanes into one
#                 extruder; here each lane feeds another tool).
#
#   [indx_afc]
#   lanes: lane1, lane2, lane3, lane4   # the lane feeding dock 0, 1, 2, ...
#                                       # ("-" for a dock no lane feeds)
#
# Settings: [gcode_macro _INDX_AFC] (macros/indx_afc.cfg), on the INDX page
# under Settings, or INDX_SETTING MACRO=_INDX_AFC VAR=<name> VALUE=<value>.
#
# Commands: INDX_AFC_STATUS, INDX_AFC_LOAD, INDX_AFC_UNLOAD, INDX_AFC_LOAD_ALL,
# INDX_AFC_UNLOAD_ALL, INDX_AFC_SYNC.

import inspect
import logging
import math
import sys

VERSION = "0.2.11"
EMPTY = ("", "NONE", "None", "none", None)
DEFAULTS = {"ram_relief": 0.0, "unload_chunk": 8.0, "prime_test": False, "print_check": "warn",
            "load_type": "none", "spool_join": True}
# INDX's CHANGE_TOOL and T<n> pass this to _INDX_CHANGE_TOOL (beta.11): the
# feedrate of the call is put back at the end, since a slicer may leave the F
# out of its next travel. Older betas ignore it.
KEEP_F = " KEEP_F=1"
# INDX_AFC_CALIBRATE
CAL_MAX = 3000.0           # mm: the longest path it tries by default
CAL_BACKOFF = 20.0         # mm: back off after the fast pass, then home again slowly
CAL_BACKOFF_TRIES = 3      # back off at most this many times for the switch to open
CAL_SPARE = 15.0           # mm: the ram's homing reaches this far past the measured point (see _calibrate)
NOZZLE_MAX_DEFAULT = 300.0                   # C: INDX's nozzle rating when nothing says otherwise
LATE_CHECKS = (0.0, 1.0, 3.0, 6.0, 12.0)     # s after klippy:ready: AFC's PREP runs at 2 s


class IndxAfc:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        names = [n.strip() for n in config.getlist("lanes")]
        self.dock_lane = {d: n for d, n in enumerate(names) if n and n not in ("-", "none")}
        self.lane_dock = {n: d for d, n in self.dock_lane.items()}
        if len(self.lane_dock) != len(self.dock_lane):
            raise config.error("[indx_afc] lanes: a lane is listed twice")
        # v0.1 options: kept as the defaults when the settings macro is missing
        self.cfg_defaults = dict(DEFAULTS)
        self.cfg_defaults["ram_relief"] = config.getfloat("ram_relief_mm", 0.0, minval=0.0, maxval=10.0)
        lt = (config.get("load_type", "") or "").strip().upper()
        if lt:
            self.cfg_defaults["load_type"] = lt
        self.afc = self.api = self.remap = self.sv = None
        self.enums = None
        self.lanes = {}            # dock -> AFC lane object
        self.enabled = False       # INDX has this module as its tool manager: lanes may sync
        self.why_off = "Klipper is not ready yet"
        self.loading = None        # dock being loaded (its lane syncs before INDX records the filament)
        self.unloading = None      # dock INDX_AFC_UNLOAD is unloading (it brings the lane home itself)
        self.pulled = None         # dock whose filament the unload pull took out of the gears
        self.rammed = set()        # docks whose lane is rammed to the gears, not fed yet
        self.in_load = False
        self.resume_temp = None    # nozzle target of a paused print, put back after a paused load
        self.evt = {}              # prep sensor name -> time of its last runout event
        self.events_ok = False     # this Klipper sends filament:runout / filament:insert
        self.wired = {}            # dock -> sensor name given to INDX's spool join
        self.warnings = []
        self.orig = {}             # AFC methods this module replaced
        self.prev = {}             # gcode commands this module wrapped
        self.temp_fns = {}
        self.mine = {}             # command -> the handler gcode holds for this module
        self.ready = False
        self.printer.register_event_handler("klippy:connect", self._connect)
        self.printer.register_event_handler("klippy:ready", self._ready)
        self.printer.register_event_handler("filament:runout", self._ev_sensor_runout)
        # tool_state: every change of INDX's active_tool, also the ones outside
        # a tool change (seated, released or forgotten by hand, a stopped park)
        for e, fn in (("after_pickup", self._ev_after_pickup), ("after_park", self._ev_after_park),
                      ("filament_state", self._ev_filament_state), ("stop", self._ev_reconcile),
                      ("tool_state", self._ev_reconcile),
                      ("boot", self._ev_boot), ("before_prime", self._ev_before_prime)):
            self.printer.register_event_handler("indx:" + e, fn)
        for cmd, fn, desc in (
                ("INDX_AFC_STATUS", self.cmd_STATUS, "INDX-AFC bridge: which lane feeds which tool, what is synced, the spool join, warnings"),
                ("INDX_AFC_LOAD", self.cmd_LOAD, "INDX-AFC bridge: load LANE= (or TOOL=) into its tool: pick it up, ram to the gears, LOAD_FILAMENT. TYPE= COLOR= optional (AFC's for the lane). In a paused print: the tool on the head only, then back to where the pause parked"),
                ("INDX_AFC_UNLOAD", self.cmd_UNLOAD, "INDX-AFC bridge: unload LANE= (or TOOL=): INDX_UNLOAD (the lane follows the pull), then the lane back to its load sensor"),
                ("INDX_AFC_LOAD_ALL", self.cmd_LOAD_ALL, "INDX-AFC bridge: load every lane that has filament into its empty tool, the reference tool last. TYPE= for lanes without a material in AFC"),
                ("INDX_AFC_UNLOAD_ALL", self.cmd_UNLOAD_ALL, "INDX-AFC bridge: unload every loaded tool a lane feeds"),
                ("INDX_AFC_CALIBRATE", self.cmd_CALIBRATE, "INDX-AFC bridge: measure LANE='s path (or LANE=ALL) from its load sensor to its tool's gears and save dist_hub (a little shorter, for AFC's ram). The tool must be empty. MAX= longest path to try (mm, 3000), SAVE=0 to only measure"),
                ("INDX_AFC_SYNC", self.cmd_SYNC, "INDX-AFC bridge: match AFC's loaded lane and syncs to the tool INDX has on the head (no moves), and check the bridge's commands. COLORS=1: AFC's lane colors into INDX"),
                ("_INDX_AFC_LANE_HOME", self.cmd_LANE_HOME, "Inner - INDX-AFC bridge: after an unload, TOOL='s lane back to its load sensor")):
            self.gcode.register_command(cmd, fn, desc=desc)

    # ---- startup ---------------------------------------------------------

    def _connect(self):
        self.afc = self.printer.lookup_object("AFC", None)
        if self.afc is None:
            raise self.printer.config_error("[indx_afc] needs AFC: no [AFC] section is loaded")
        self.api = self.printer.lookup_object("indx_api", None)
        if self.api is None:
            raise self.printer.config_error("[indx_afc] needs the INDX Suite (indx_api): install INDX with install.sh, not --macros-only")
        self.remap = self.printer.lookup_object("indx_remap", None)
        self.sv = self.printer.lookup_object("save_variables", None)
        # AFC's own tool change, load and unload: redirected (instance
        # attributes shadow the class methods; AFC's command handlers call
        # them through self, so its T<n> and panel buttons land here too)
        for name, fn in (("CHANGE_TOOL", self._afc_change_tool), ("TOOL_LOAD", self._afc_tool_load),
                         ("TOOL_UNLOAD", self._afc_tool_unload), ("LANE_UNLOAD", self._afc_lane_unload)):
            orig = getattr(self.afc, name, None)
            if orig is None:
                raise self.printer.config_error("[indx_afc] this AFC has no %s: update AFC or this bridge" % name)
            self.orig[name] = orig
            setattr(self.afc, name, fn)
        # AFC's PAUSE and RESUME (it registers them at its PREP, 2 s after
        # the start, by these attributes): no Z hop and no position restore
        # of AFC's own. INDX parks and comes back itself; AFC's restore would
        # put back the G-code offsets of the tool that was on the head at the
        # pause, and INDX holds prints with the base PAUSE, behind AFC's back
        err = getattr(self.afc, "error", None)
        if err is not None and hasattr(err, "cmd_AFC_PAUSE") and hasattr(err, "cmd_AFC_RESUME"):
            self.orig["AFC_PAUSE"], self.orig["AFC_RESUME"] = err.cmd_AFC_PAUSE, err.cmd_AFC_RESUME
            err.cmd_AFC_PAUSE, err.cmd_AFC_RESUME = self._afc_pause, self._afc_resume
        else:
            self._warn("this AFC has no cmd_AFC_PAUSE / cmd_AFC_RESUME: its PAUSE and RESUME wrappers are left as they are (check a pause and resume before printing)")
        self._shim_remap()

    def _shim_remap(self):
        """INDX's tool mapping and spool join with this tool manager. INDX
        turns both off for any manager (beta.7: it also leaves every runout
        to the manager); this one only feeds the tools (it does not own T<n>)
        and lets INDX's spool join decide. Before klippy:ready, so INDX's own
        start sees it."""
        r = self.remap
        if r is None:
            return
        if hasattr(r, "_manager"):
            orig = r._manager

            def manager():
                m = orig()
                if m is not None and getattr(m, "name", "") == "indx_afc" and not getattr(m, "owns_tool_commands", False) \
                        and self._setting("spool_join"):
                    return None
                return m
            r._manager = manager
        # beta.5: a runout that arrives while its tool is being picked up is
        # only mapped away after the change, and the tool prints on empty.
        # The join starts then instead.
        if hasattr(r, "_watch") and hasattr(r, "join_busy") and hasattr(r, "_start_join") and not self._remap_fixed():
            watch = r._watch

            def watched(eventtime):
                jb = r.join_busy
                ret = watch(eventtime)
                if jb is not None and r.join_busy is None:
                    frm, to = jb
                    try:
                        if r._active_tool() == frm and r._print_state() == "printing":
                            self.reactor.register_callback(lambda e: self._start_join(frm, to))
                    except Exception:
                        logging.exception("INDX-AFC: spool join after a tool change")
                return ret
            r._watch = watched

    def _remap_fixed(self):
        """INDX beta.7 and later: its spool join runs PAUSE and the join after
        the command (_runout(self, d) with no gcmd), decides a runout held
        during a tool change itself, and starts a queued join after
        _INDX_REMAP_JOINED. The bridge's beta.5/6 workarounds then stand aside."""
        fn = getattr(self.remap, "_runout", None)
        code = getattr(fn, "__code__", None)
        if code is None:
            return False
        return "gcmd" not in code.co_varnames[:code.co_argcount]

    def _start_join(self, frm, to):
        try:
            self.remap._start_join(frm, to)
        except Exception:
            logging.exception("INDX-AFC: spool join T%s to T%s", frm, to)

    def _ready(self):
        self.warnings = [w for w in self.warnings if w.startswith("this AFC has no cmd_AFC")]
        lanes = getattr(self.afc, "lanes", {}) or {}
        for d, n in self.dock_lane.items():
            lane = lanes.get(n)
            if lane is None:
                raise self.printer.config_error("[indx_afc] lanes: AFC has no lane %s (its lanes: %s)" % (n, ", ".join(lanes) or "none"))
            self.lanes[d] = lane
        self.enums = _afc_enums(next(iter(self.lanes.values()), None))
        # lanes pushed to their tool before a restart, and not loaded since
        try:
            saved = self._vars().get("indx_afc_rammed", []) or []
            self.rammed = set(int(x) for x in saved if int(x) in self.lanes)
        except Exception:
            self.rammed = set()
        no_heat = self._no_heat_tools()
        for d in sorted(self.lanes):
            if d in no_heat:
                raise self.printer.config_error(
                    "[indx_afc] lanes: %s feeds T%d, but T%d is in INDX's no_heat_tools (a tool without a hotend "
                    "cannot take filament). Give that lane no tool (leave its place empty) or take T%d out of no_heat_tools."
                    % (self.dock_lane[d], d, d, d))
        for d, lane in self.lanes.items():
            self._wrap_lane(lane)
            self._check_lane(d, lane)
        self._check_afc()
        n_tools = self._tool_count()
        for d in self.lanes:
            if n_tools is not None and d >= n_tools:
                self._warn("lane %s is set for T%d, but INDX has %d docks" % (self.dock_lane[d], d, n_tools))
        m = getattr(self.api, "manager", None)
        if m is None or getattr(m, "name", "") != "indx_afc":
            self.enabled = False
            self.why_off = ("INDX does not have indx_afc as its tool manager%s (run this bridge's install.sh so "
                            "indx/ext/indx_afc.cfg is included)" % ((": it is %s" % m.name) if m is not None else ""))
            self._warn("lanes will NOT be synced: " + self.why_off)
        else:
            self.enabled = True
            self.why_off = ""
        self.ready = True
        # After every module's klippy:ready (AFC renames M104/M109 in its
        # own, INDX registers CHANGE_TOOL in its own) and again after AFC's
        # PREP: this module's commands, the sensors, AFC matched to INDX
        now = self.reactor.monotonic()
        for dt in LATE_CHECKS:
            self.reactor.register_callback(self._late, now + dt)
        for w in self.warnings:
            self.gcode.respond_info("INDX-AFC ! " + w)

    def _late(self, eventtime):
        try:
            self._install_commands()
            self._wire_sensors()
            # inside the G-code lock: AFC sets motor currents with gcode, and
            # Kalico refuses a script from a command outside the lock
            self.gcode.run_script("INDX_AFC_SYNC QUIET=1")
        except Exception:
            logging.exception("INDX-AFC: start")

    def _wrap_lane(self, lane):
        if getattr(lane, "_indx_afc_wrapped", False):
            return
        lane._indx_afc_wrapped = True
        for name in ("_perform_pause_runout", "_perform_infinite_runout"):
            orig = getattr(lane, name, None)
            if orig is not None:
                setattr(lane, name, (lambda o=orig, n=name, l=lane: self._lane_runout(l, o, n)))

    @staticmethod
    def _lane_map(lane):
        """AFC main keeps a lane's map as a string ("T0"), AFC DEV as a list
        (["T0"]): its T commands, as a list."""
        m = getattr(lane, "map", None)
        if m is None:
            return []
        if isinstance(m, str):
            return [x.strip() for x in m.split(",") if x.strip()]
        try:
            return [str(x).strip() for x in m]
        except TypeError:
            return [str(m)]

    def _maps_to(self, lane, d):
        return self._lane_map(lane) == ["T%d" % d]

    def _check_lane(self, d, lane):
        n = lane.name
        if getattr(lane, "hub", None) != "direct":
            self._warn("%s: set hub: direct in [AFC_stepper %s] (one lane, one tool, no shared hub; not direct_load)" % (n, n))
        if not self._maps_to(lane, d):
            self._warn("%s: set map: T%d in [AFC_stepper %s] (now %s): AFC then takes T%d over from INDX's macro and hands it to the bridge"
                       % (n, d, n, getattr(lane, "map", None), d))
        if getattr(lane, "buffer_obj", None) is None:
            self._warn("%s: no buffer (buffer: in [AFC_stepper %s]): no ram sensor and no speed trim for this lane; it cannot load" % (n, n))
        ext = getattr(lane, "extruder_obj", None)
        if ext is not None and getattr(ext, "tool_start", None) != "buffer":
            self._warn("[AFC_extruder %s]: set pin_tool_start: buffer (INDX tools have no toolhead sensor; the load rams to the gears)" % ext.name)
        if getattr(lane, "runout_lane", None):
            self._warn("%s: AFC infinite spool (SET_RUNOUT) is set; this bridge ignores it, INDX's spool join decides" % n)
        if getattr(getattr(lane, "unit_obj", None), "unload_on_runout", False):
            self._warn("%s's unit has unload_on_runout: True; set it False (an unload mid-print goes through INDX)" % n)

    def _check_afc(self):
        if not getattr(self.afc, "homing_enabled", True) or not getattr(self.afc, "home_to_tool", True):
            self._warn("[AFC] homing_enabled and home_to_tool must be True: the load rams the lane against the tool's gears until its buffer triggers")
        prep = self.printer.lookup_object("AFC_prep", None)
        if prep is not None and getattr(prep, "enable", False):
            self._warn("[AFC_prep] enable: True moves every lane 5 mm out and back at each Klipper start, also the lanes whose filament sits in a parked tool. The buffers take it; enable: False avoids it")

    def _warn(self, msg):
        if msg in self.warnings:
            return
        self.warnings.append(msg)
        logging.warning("INDX-AFC: %s", msg)

    # ---- wrapped gcode commands --------------------------------------------

    def _wrap_cmd(self, name, fn, desc):
        """Put fn(gcmd, previous handler) in front of an existing command.
        Checked again later: AFC and INDX register some at their own start."""
        handlers = getattr(self.gcode, "ready_gcode_handlers", {})
        cur = handlers.get(name)
        if cur is not None and cur is self.mine.get(name):
            return True
        prev = self.gcode.register_command(name, None)
        if prev is None:
            return False

        def handler(gcmd, p=prev):
            return fn(gcmd, p)
        self.gcode.register_command(name, handler, desc=desc)
        # what gcode holds now (Kalico wraps a handler in its own parameter
        # parser): how a later check knows the command is still the bridge's
        self.mine[name] = handlers.get(name, handler)
        self.prev[name] = prev
        return True

    def _install_commands(self):
        # M104/M109: AFC's (registered in its klippy:ready) heat the shared
        # extruder for any T. INDX stepped aside (owns_temp_commands).
        for cmd, wait in (("M104", 0), ("M109", 1)):
            fn = self.temp_fns.get(cmd)
            if fn is None:
                fn = self.temp_fns[cmd] = (lambda gcmd, w=wait: self._cmd_temp(gcmd, w))
            if getattr(self.gcode, "ready_gcode_handlers", {}).get(cmd) is not fn:
                self.gcode.register_command(cmd, None)
                self.gcode.register_command(cmd, fn, desc="INDX-AFC: nozzle temperature with INDX's heat rules (INDX_SET_TEMP); "
                                                          "a T for a tool that is not on the head is ignored")
        missing = []
        for name, fn, desc in (
                # AFC owns CHANGE_TOOL (LANE=): INDX's public form, TOOL=, still works
                ("CHANGE_TOOL", self._cmd_change_tool, "Change to TOOL=n (INDX), or AFC's CHANGE_TOOL LANE=<lane>: both end in INDX's tool change"),
                ("LOAD_FILAMENT", self._cmd_load_filament, "INDX's LOAD_FILAMENT; with a lane feeding TOOL=: the lane rams its filament to the gears first and follows the load (INDX-AFC bridge). RAM=0 skips the ram, RAM=1 forces it"),
                ("INDX_LOAD_FILAMENT", self._cmd_indx_load_filament, "INDX's feed to the melt zone and prime; in a bridge load the lane is synced to the extruder right before it (INDX-AFC bridge)"),
                ("_INDX_UNLOAD_PULL", self._cmd_unload_pull, "Inner - INDX_UNLOAD's pull out of the gears; with a lane feeding the tool: in parts, the lane taking each back (INDX-AFC bridge)"),
                ("INDX_RUNOUT", self._cmd_indx_runout, "A spool ran out in DOCK=<n> (PRESENT=1: loaded again): INDX's spool join, or a pause without a partner. Runs after this command (INDX-AFC bridge: the stock one waits on its own G-code lock)"),
                ("_INDX_REMAP_JOINED", self._cmd_joined, "Inner - a spool join's change is back at the print point (INDX-AFC bridge: a queued join starts after this command)")):
            if name in ("INDX_RUNOUT", "_INDX_REMAP_JOINED") and (self.remap is None or self._remap_fixed()):
                continue
            if not self._wrap_cmd(name, fn, desc):
                missing.append(name)
        for name in missing:
            self._warn("INDX has no %s command to wrap: this bridge was written for beta.11, check for an update (INDX_AFC_STATUS)" % name)

    def _cmd_change_tool(self, gcmd, prev):
        p = gcmd.get_command_parameters()
        if "TOOL" in p and "LANE" not in p:
            # KEEP_F=1 last, as INDX's own CHANGE_TOOL: the print's feedrate
            # is put back after the change (beta.11; older betas ignore it)
            self.gcode.run_script_from_command(("_INDX_CHANGE_TOOL " + gcmd.get_raw_command_parameters()).strip() + KEEP_F)
            return
        return prev(gcmd)

    def _cmd_indx_runout(self, gcmd, prev):
        if gcmd.get_int("PRESENT", 0):
            return prev(gcmd)
        d = gcmd.get_int("DOCK", minval=0)
        gcmd.respond_info("INDX: T%d's spool ran out" % d)
        self.reactor.register_callback(lambda e: self._indx_runout(d))

    def _cmd_joined(self, gcmd, prev):
        r = self.remap
        nxt = getattr(r, "join_next", None)
        if not nxt:
            return prev(gcmd)
        # a second runout queued during the first join: INDX starts it with
        # run_script inside this command (its own G-code lock). After it.
        r.join_next = {}
        prev(gcmd)
        frm, to = nxt.get("from"), nxt.get("to")
        self.reactor.register_callback(lambda e: self._start_join(frm, to))

    # ---- settings and INDX state -------------------------------------------

    def _setting(self, name):
        m = self.printer.lookup_object("gcode_macro _INDX_AFC", None)
        v = getattr(m, "variables", {}).get(name) if m is not None else None
        return self.cfg_defaults.get(name) if v is None else v

    def _macro_var(self, macro, name, default):
        m = self.printer.lookup_object("gcode_macro " + macro, None)
        v = getattr(m, "variables", {}).get(name) if m is not None else None
        return default if v is None else v

    def _set_rammed(self, d, on):
        """The docks whose lane is pushed to the tool but not loaded yet. Kept
        in save_variables (indx_afc_rammed) too: after a restart the bridge
        must not push that lane again (AFC's push starts with a full speed
        move that does not watch the buffer)."""
        before = set(self.rammed)
        (self.rammed.add if on else self.rammed.discard)(d)
        if self.rammed != before and self.printer.lookup_object("save_variables", None) is not None:
            try:
                self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=indx_afc_rammed VALUE=[%s]" % ",".join(str(x) for x in sorted(self.rammed)))
            except Exception:
                logging.exception("INDX-AFC: could not save indx_afc_rammed")

    def _vars(self):
        if self.sv is None:
            return {}
        return getattr(self.sv, "allVariables", {}) or {}

    def _status(self):
        try:
            return self.api.get_status(self.reactor.monotonic()) or {}
        except Exception:
            logging.exception("INDX-AFC: indx_api status")
            return {}

    def _active_tool(self):
        a = self._status().get("active_tool")
        return a if isinstance(a, int) else None

    def _no_heat_tools(self):
        try:
            return set(int(t) for t in (self._status().get("no_heat_tools") or []))
        except (TypeError, ValueError):
            return set()

    def _nozzle_max(self):
        """INDX's nozzle rating, C: printer.indx_api (beta.6+), else
        _INDX_CONSTANTS, else INDX's default of 300."""
        for v in (self._status().get("nozzle_max_temp"), self._macro_var("_INDX_CONSTANTS", "nozzle_max_temp", None)):
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
        return NOZZLE_MAX_DEFAULT

    def _tool_count(self):
        try:
            return int(self._status().get("tool_count"))
        except (TypeError, ValueError):
            return None

    def _has_filament(self, d):
        return self._vars().get("t%d_material" % d) not in EMPTY

    def _print_state(self):
        ps = self.printer.lookup_object("print_stats", None)
        return ps.get_status(self.reactor.monotonic()).get("state") if ps is not None else None

    def _paused(self):
        pr = self.printer.lookup_object("pause_resume", None)
        return bool(pr.get_status(self.reactor.monotonic()).get("is_paused")) if pr is not None else False

    def _slicer_to_dock(self, k):
        # INDX's view of a slicer tool number (its tool mapping and spool
        # joins), the same view PRINT_TOOLCHANGE uses
        if self.remap is not None:
            try:
                return int(self.remap.slicer_to_dock(k))
            except Exception:
                logging.exception("INDX-AFC: slicer_to_dock")
        return k

    def _presets(self):
        # beta.7: printer.indx_api reports them; LOAD_FILAMENT's variable before
        names = self._status().get("filament_presets")
        if isinstance(names, (list, tuple)) and names:
            return [str(k).upper() for k in names]
        p = self._macro_var("LOAD_FILAMENT", "filament_presets", None)
        return [str(k).upper() for k in p] if isinstance(p, dict) and p else ["PLA", "PETG", "ABS", "ASA", "PA", "PC", "TPU"]

    # ---- lane sync ---------------------------------------------------------

    def _suspect(self, d):
        """INDX keeps this tool recorded on the head without proof (beta.6:
        a stopped park, "T<n> may still be on the head"; INDX's tools[n]
        state attention). It may be in its dock: its lane stays unsynced
        until a person says what is on the head."""
        tools = self._status().get("tools")
        try:
            return tools[d].get("state") == "attention"
        except (TypeError, IndexError, KeyError, AttributeError):
            return False

    def _desired_dock(self):
        act = self._active_tool()
        if act is None or act < 0 or act not in self.lanes:
            return None
        if self._suspect(act):
            return None
        if self.loading == act or self._has_filament(act):
            return act
        return None

    def _apply(self, d):
        """Make AFC agree: the lane of dock d tool-loaded and synced (d None:
        none). No moves. AFC's handle_activate_extruder unsyncs and idles every
        other lane and syncs, enables and buffers the loaded one."""
        f = self.afc.function
        want = self.lanes.get(d) if d is not None else None
        if want is not None and not self.enabled:
            want = None
        cur = f.get_current_lane_obj()
        if cur is want:
            if want is not None:
                self._sync_lane(want)
            else:
                for lane in self.lanes.values():
                    if getattr(lane, "_synced_to_extruder", False):
                        lane.unsync_to_extruder()
            return
        if cur is not None:
            cur.unsync_to_extruder()
            cur.set_tool_unloaded(normal_toolchange=True)
            if cur.prep_state and cur.load_state:
                # still in its tool, parked: a loaded lane for AFC, not an empty one
                cur.status = self.enums[2].LOADED
        if want is not None:
            want.set_tool_loaded(normal_toolchange=True)
        f.handle_activate_extruder()
        self.afc.save_vars()

    def _lane_follows(self, lane):
        """Is the lane's stepper on the extruder's motion queue (Klipper's
        own state, not AFC's record of it)? None: cannot tell."""
        try:
            drive = getattr(lane, "drive_stepper", None) or lane
            tq = self.printer.lookup_object("toolhead").get_extruder().get_trapq()
            return drive.extruder_stepper.stepper.get_trapq() == tq
        except Exception:
            return None

    def _sync_lane(self, lane):
        """Sync the lane to the extruder and make sure it took. AFC skips the
        sync when its own record says the lane is synced already; something
        that unsynced the stepper without AFC (a latch move whose sync back
        went elsewhere, for one) leaves that record wrong, and the lane would
        stand still while the extruder feeds. Returns False if the lane is
        still not on the extruder's motion queue."""
        # a synced lane whose motor is off follows nothing: AFC switches a
        # lane's motor off after many of its own moves
        lane.do_enable(True)
        lane.sync_to_extruder()
        if self._lane_follows(lane) is False:
            drive = getattr(lane, "drive_stepper", None) or lane
            logging.info("INDX-AFC: %s was not on the extruder although AFC had it synced: syncing again", lane.name)
            for o in (drive, lane):
                if hasattr(o, "_synced_to_extruder"):
                    o._synced_to_extruder = None if not isinstance(getattr(o, "_synced_to_extruder"), bool) else False
            lane.sync_to_extruder()
        return self._lane_follows(lane) is not False

    def _cmd_indx_load_filament(self, gcmd, prev):
        """INDX's feed until the filament reaches the melt zone: the lane of
        the tool being loaded must follow it. Synced (and checked) right here,
        whatever happened since the ram (a pickup, a latch move, an event)."""
        d = self.loading
        lane = self.lanes.get(d) if d is not None else None
        if lane is not None and self.enabled and self._active_tool() == d:
            self._apply(d)
            if not self._sync_lane(lane):
                raise gcmd.error("INDX-AFC: %s could not be synced to the extruder, so it would not follow T%d's load. "
                                 "Not feeding. Run INDX_AFC_SYNC and try again; klippy.log has more" % (lane.name, d))
            gcmd.respond_info("INDX-AFC: %s follows T%d's load" % (lane.name, d))
        return prev(gcmd)

    def _reconcile(self):
        if self.afc is None or not self.lanes or self.enums is None:
            return
        try:
            self._apply(self._desired_dock())
        except Exception:
            logging.exception("INDX-AFC: reconcile")
            self.gcode.respond_info("INDX-AFC ! could not match AFC's lanes to the tool on the head (klippy.log). Run INDX_AFC_SYNC before extruding.")

    def _ev_after_pickup(self, p):
        try:
            d = int(float(p.get("TOOL", -1)))
        except (TypeError, ValueError):
            d = -1
        # raises on a failed sync: INDX then stops, rather than printing with
        # a lane that does not follow
        self._apply(d if (d in self.lanes and (self.loading == d or self._has_filament(d))) else None)

    def _ev_after_park(self, p):
        self._apply(None)

    def _ev_reconcile(self, p):
        # outside a change, or at its next fire point: INDX's state settled
        self._reconcile()

    def _ev_boot(self, p):
        # INDX restored its saved state and settings (5 s after a restart)
        self._wire_sensors()
        self._reconcile()

    def _ev_filament_state(self, p):
        try:
            d = int(float(p.get("TOOL", -1)))
            loaded = int(float(p.get("LOADED", 1)))
        except (TypeError, ValueError):
            d, loaded = -1, 1
        if loaded:
            self._set_rammed(d, False)
        self._reconcile()
        if not loaded and self.pulled == d and self.unloading != d:
            # INDX's own unload (its page, INDX_UNLOAD) pulled the filament
            # out of the gears: the lane takes the rest back when it is done
            self.reactor.register_callback(lambda e, d=d: self._run_locked("_INDX_AFC_LANE_HOME TOOL=%d" % d))

    def _run_locked(self, script):
        try:
            self.gcode.run_script(script)
        except Exception:
            logging.exception("INDX-AFC: %s", script)

    # ---- AFC redirects -----------------------------------------------------

    # AFC DEV passes more arguments than main (gcmd, load_to_gears,
    # restore_temp), some by position: every redirect takes *args, **kw
    def _afc_change_tool(self, cur_lane, purge_length=None, restore_pos=True, new_extruder_temp=None, *args, **kw):
        k = self.lane_dock.get(cur_lane.name)
        if k is None:
            raise self.gcode.error("INDX-AFC: %s feeds no INDX tool ([indx_afc] lanes), not changing" % cur_lane.name)
        # as INDX's own T<n>: the slicer's number through INDX's tool map
        cmd = "_INDX_CHANGE_TOOL TOOL=%d" % self._slicer_to_dock(k)
        if new_extruder_temp is not None:
            cmd += " TEMP=%s" % new_extruder_temp
        self.gcode.run_script_from_command(cmd + KEEP_F)

    def _afc_tool_load(self, cur_lane, purge_length=None, set_start_time=False, *args, **kw):
        d = self.lane_dock.get(cur_lane.name)
        if d is None:
            self.afc.error.AFC_error("INDX-AFC: %s feeds no INDX tool, not loading" % cur_lane.name, pause=False)
            return False
        self._load(d)
        return True

    def _afc_tool_unload(self, cur_lane, set_start_time=True, force_unload=False, *args, **kw):
        d = self.lane_dock.get(cur_lane.name)
        if d is None:
            self.afc.error.AFC_error("INDX-AFC: %s feeds no INDX tool, not unloading" % cur_lane.name, pause=False)
            return False
        self._unload(d)
        return True

    def _afc_lane_unload(self, cur_lane, *args, **kw):
        d = self.lane_dock.get(cur_lane.name)
        if d is not None and self._has_filament(d):
            self.afc.error.AFC_error(
                "INDX-AFC: T%d still has %s's filament in it. Unload it first (INDX_AFC_UNLOAD LANE=%s, or the "
                "Unload button): ejecting now would pull it out of a cold nozzle." % (d, cur_lane.name, cur_lane.name), pause=False)
            return
        if d is not None:
            self._set_rammed(d, False)
        return self.orig["LANE_UNLOAD"](cur_lane, *args, **kw)

    def _afc_pause(self, gcmd):
        err = self.afc.error
        self.gcode.run_script_from_command(("%s %s" % (err.AFC_RENAME_PAUSE_NAME, gcmd.get_raw_command_parameters())).strip())

    def _afc_resume(self, gcmd):
        err = self.afc.error
        self.afc.in_toolchange = False
        self.gcode.run_script_from_command(("%s %s" % (err.AFC_RENAME_RESUME_NAME, gcmd.get_raw_command_parameters())).strip())
        self.resume_temp = None
        try:
            err.reset_failure()
        except Exception:
            logging.exception("INDX-AFC: AFC reset_failure")

    # ---- load --------------------------------------------------------------

    def _cmd_load_filament(self, gcmd, prev):
        t = gcmd.get_int("TOOL", None)
        lane = self.lanes.get(t) if t is not None else None
        if lane is None or not self.enabled or self.in_load:
            return prev(gcmd)
        ram = gcmd.get_int("RAM", -1)
        if t in self.rammed and not lane.load_state:
            self._set_rammed(t, False)    # its filament is gone from the lane since
        need = (not self._has_filament(t)) and t not in self.rammed
        if need and not lane.load_state and ram != 1:
            gcmd.respond_info("INDX-AFC: %s has no filament at its load sensor: T%d loads as INDX does by hand (push the filament in to the gears)"
                              % (lane.name, t))
            need = False
        self.in_load = True
        try:
            if ram == 1 or (ram != 0 and need):
                self._pickup_and_ram(t)
            # the lane follows the load from here: LOAD_FILAMENT's feed, prime
            # and tip shaping pull it along, its buffer trimming it
            self.loading = t
            if self._active_tool() == t:
                self._apply(t)
            prev(gcmd)
            self._set_rammed(t, False)
        finally:
            self.loading = None
            self.in_load = False
            self._reconcile()

    def _pickup_and_ram(self, d):
        lane = self.lanes[d]
        SM, AA, ST, MW = self.enums
        if lane.buffer_obj is None or getattr(lane.extruder_obj, "tool_start", None) != "buffer":
            raise self.gcode.error("INDX-AFC: %s cannot ram: it needs its own buffer (buffer:) and pin_tool_start: buffer in [AFC_extruder]" % lane.name)
        if not getattr(self.afc, "homing_enabled", True) or not getattr(self.afc, "home_to_tool", True):
            raise self.gcode.error("INDX-AFC: %s cannot ram: [AFC] homing_enabled and home_to_tool must be True" % lane.name)
        if not lane.load_state:
            raise self.gcode.error("INDX-AFC: %s has no filament at its load sensor: insert the spool first" % lane.name)
        # 1. The empty tool onto the head first: filament pushed into a docked
        #    tool's gears is a misload for INDX
        self._pickup_empty(d, "ramming")
        # 2. Ram the filament to the gears, the lane unsynced (nothing is
        #    synced: the tool is still empty)
        self._apply(None)
        self.gcode.respond_info("INDX-AFC: %s to T%d's gears (%.0f mm at most, until its buffer triggers)" % (lane.name, d, lane.dist_hub))
        lane.do_enable(True)
        homed, moved, warn = lane.unit_obj.load_then_home(lane, lane.dist_hub, AA.DYNAMIC, lane.get_toolhead_endstop())
        if warn == MW.ERROR or not lane.get_toolhead_pre_sensor_state():
            lane.do_enable(False)
            raise self.gcode.error("INDX-AFC: %s did not reach T%d's gears (moved %.0f mm, the buffer did not see the ram). "
                                   "Check the path and dist_hub, then try again." % (lane.name, d, moved or 0.0))
        relief = float(self._setting("ram_relief") or 0.0)
        if relief > 0:
            lane.move_advanced(-relief, SM.SHORT)
        lane.loaded_to_hub = True
        self._set_rammed(d, True)

    def _pickup_empty(self, d, what):
        """The empty tool d onto the head (its gears hold still: what a ram
        stops against). Filament pushed into a docked tool's gears would be
        a misload for INDX."""
        if self._active_tool() != d:
            self.gcode.run_script_from_command("INDX_CHANGE_TOOL TOOL=%d RETURN=none" % d)
        self.printer.lookup_object("toolhead").wait_moves()
        if self._active_tool() != d:
            raise self.gcode.error("INDX-AFC: T%d is not on the head after the pickup; not %s" % (d, what))

    def _material(self, lane, given):
        presets = self._presets()
        t = (given or "").strip().upper()
        if not t:
            m = str(getattr(lane, "material", "") or "").strip().upper()
            # the longest preset first: "PA" must not take "PA-CF" from a "PA-CF" preset
            for p in sorted(presets, key=len, reverse=True):
                if m == p or (m.startswith(p) and not m[len(p):len(p) + 1].isalnum()):
                    t = p
                    break
        if not t:
            lt = str(self._setting("load_type") or "").strip().upper()
            t = lt if lt and lt != "NONE" else ""
        if t and t not in presets:
            raise self.gcode.error("INDX-AFC: %s is not one of INDX's filament presets (%s)" % (t, ", ".join(presets)))
        return t

    def _color(self, lane, given):
        c = (given or str(getattr(lane, "color", "") or "")).strip().lstrip("#").lower()[:6]
        return c if len(c) == 6 and all(ch in "0123456789abcdef" for ch in c) else ""

    def _gate(self, what, d):
        """Loads and unloads: never while printing; in a paused print only
        the tool on the head. Returns True for a paused print."""
        if not self.enabled:
            raise self.gcode.error("INDX-AFC: no %s, %s" % (what, self.why_off))
        st = self._print_state()
        paused = self._paused()
        if st == "printing" and not paused:
            raise self.gcode.error("INDX-AFC: no %s while printing. PAUSE first: a paused print can %s the tool on the head." % (what, what))
        if paused and self._active_tool() != d:
            a = self._active_tool()
            raise self.gcode.error("INDX-AFC: the print is paused with %s on the head: only that tool can %s now (T%d is parked)."
                                   % (("T%d" % a) if a is not None and a >= 0 else "no tool", what, d))
        return paused

    def _load(self, d, typ="", color=""):
        lane = self.lanes[d]
        paused = self._gate("load", d)
        if self._has_filament(d):
            raise self.gcode.error("INDX-AFC: T%d already has filament (INDX records %s). Unload it first."
                                   % (d, self._vars().get("t%d_material" % d)))
        if not lane.load_state:
            raise self.gcode.error("INDX-AFC: %s has no filament at its load sensor: insert the spool first" % lane.name)
        t = self._material(lane, typ)
        if not t:
            raise self.gcode.error("INDX-AFC: which material is in %s? Give TYPE= (%s), set it on the spool in AFC, or set the Load material setting"
                                   % (lane.name, ", ".join(self._presets())))
        col = self._color(lane, color)
        self.gcode.respond_info("INDX-AFC: loading %s into T%d (%s%s)" % (lane.name, d, t, (", #" + col) if col else ""))
        cmd = "LOAD_FILAMENT TOOL=%d TYPE=%s" % (d, t)
        if col:
            cmd += " COLOR=%s" % col
        back = None
        if paused:
            # the print goes on from a full nozzle: no tip shaping retraction
            cmd += " TIP=0"
            back = self._pause_enter()
        self.gcode.run_script_from_command(cmd)      # the wrapped LOAD_FILAMENT: ram, sync, INDX's load
        self.afc.save_vars()
        if paused:
            self._pause_leave(back, reheat=True)
            self.gcode.respond_info("INDX-AFC: %s loaded into T%d. RESUME when the nozzle is back at temperature." % (lane.name, d))
            return
        self.gcode.respond_info("INDX-AFC: %s loaded into T%d" % (lane.name, d))
        if self._setting("prime_test") and self.printer.lookup_object("gcode_macro INDX_PRIME_TEST", None) is not None:
            self.gcode.respond_info("INDX-AFC: prime test of T%d (the flow check reads the extrusion force)" % d)
            self.gcode.run_script_from_command("INDX_PRIME_TEST")

    # ---- unload ------------------------------------------------------------

    def _cmd_unload_pull(self, gcmd, prev):
        t = gcmd.get_int("TOOL", self._active_tool() if self._active_tool() is not None else -1)
        lane = self.lanes.get(t)
        if lane is None or not self.enabled:
            return prev(gcmd)
        SM = self.enums[0]
        length = abs(float(self._macro_var("INDX_UNLOAD", "unload_length", 65.0)))
        speed = float(self._macro_var("INDX_UNLOAD", "unload_speed", 15.0))
        cur = self._macro_var("_INDX_CONSTANTS", "run_current", None)
        chunk = min(max(float(self._setting("unload_chunk") or 8.0), 2.0), 20.0)
        n = max(1, int(math.ceil(length / chunk)))
        step = length / n
        # INDX unsyncs the lane for a latch move, and this pull is one: the
        # tool's gears would push 65 mm back into a lane that stands still.
        # In parts no longer than the buffer takes, the lane following each.
        lane.unsync_to_extruder()
        lane.do_enable(True)
        self.gcode.respond_info("INDX-AFC: T%d out of its gears, %.0f mm in %d parts, %s following" % (t, length, n, lane.name))
        for _ in range(n):
            self.gcode.run_script_from_command("_TC_LATCH_MOVE E=-%.3f F=%.0f%s" % (step, speed * 60.0, (" I=%s" % cur) if cur is not None else ""))
            lane.move_advanced(-step, SM.SHORT)
        self.gcode.run_script_from_command("M400")
        self.pulled = t

    def _lane_home(self, d):
        """The lane back to its load sensor, unsynced: after the filament is
        out of the tool's gears."""
        lane = self.lanes[d]
        SM, AA, ST, MW = self.enums
        if self.afc.function.get_current_lane_obj() is lane:
            self._apply(None)
        lane.unsync_to_extruder()
        lane.do_enable(True)
        _, _, warn = lane.move_to(lane.dist_hub * -1, SM.LONG, assist_active=AA.DYNAMIC,
                                  endstop=lane.load_es, use_homing=self.afc.homing_enabled)
        self.pulled = None
        self._set_rammed(d, False)
        if warn == MW.ERROR:
            lane.do_enable(False)
            raise self.gcode.error("INDX-AFC: %s did not get back to its load sensor" % lane.name)
        tries = 0
        while not lane.raw_load_state and lane.prep_state and tries < 5:
            tries += 1
            lane.move_advanced(lane.short_move_dis, SM.SHORT)
        lane.loaded_to_hub = False
        lane.status = ST.LOADED if (lane.prep_state and lane.load_state) else ST.NONE
        lane.unit_obj.lane_tool_unloaded(lane)
        lane.do_enable(False)
        self.afc.save_vars()

    def _unload(self, d):
        lane = self.lanes[d]
        paused = self._gate("unload", d)
        back = self._pause_enter() if paused else None
        if self._has_filament(d):
            # INDX picks the tool up (its lane syncs at the pickup), shapes the
            # tip and pulls out of the gears (_INDX_UNLOAD_PULL, wrapped)
            self.unloading = d
            try:
                self.gcode.run_script_from_command("INDX_UNLOAD TOOL=%d CONFIRM=1" % d)
            finally:
                self.unloading = None
            self.printer.lookup_object("toolhead").wait_moves()
            if self._has_filament(d):
                raise self.gcode.error("INDX-AFC: INDX_UNLOAD did not finish for T%d (still loaded); %s stays where it is" % (d, lane.name))
        self._lane_home(d)
        if paused:
            self._pause_leave(back, reheat=False)
        self.gcode.respond_info("INDX-AFC: T%d unloaded, %s back at its load sensor%s" % (
            d, lane.name, "" if lane.prep_state else " (its spool is out: take the end out of the lane)"))

    # ---- a paused print ----------------------------------------------------

    def _pause_enter(self):
        gm = self.printer.lookup_object("gcode_move")
        pos = list(gm.get_status(self.reactor.monotonic()).get("gcode_position"))[:3]
        try:
            target = float(self.printer.lookup_object("toolhead").get_extruder().get_heater()
                           .get_status(self.reactor.monotonic()).get("target", 0.0))
        except Exception:
            target = 0.0
        if target > 0 and self.resume_temp is None:
            self.resume_temp = target
        return pos

    def _pause_leave(self, pos, reheat):
        """Back to where the pause parked the head, Z first, so RESUME finds
        it there: INDX's load and unload end over the purge lane."""
        if pos is not None:
            cfg = self.printer.lookup_object("toolhead").get_status(self.reactor.monotonic())
            f = min(float(cfg.get("max_velocity", 150.0) or 150.0), 150.0) * 60.0
            gm = self.printer.lookup_object("gcode_move")
            z = float(list(gm.get_status(self.reactor.monotonic()).get("gcode_position"))[2])
            lines = ["SAVE_GCODE_STATE NAME=_indx_afc_pause", "G90"]
            if pos[2] > z:
                lines.append("G1 Z%.3f F900" % pos[2])
            lines += ["G1 X%.3f Y%.3f F%.0f" % (pos[0], pos[1], f), "G1 Z%.3f F900" % pos[2],
                      "RESTORE_GCODE_STATE NAME=_indx_afc_pause"]
            self.gcode.run_script_from_command("\n".join(lines))
        if reheat and self.resume_temp:
            self.gcode.respond_info("INDX-AFC: nozzle back to the print's %.0f C" % self.resume_temp)
            self.gcode.run_script_from_command("INDX_SET_TEMP TARGET=%.1f WAIT=0 SRC=INDX_AFC" % self.resume_temp)

    # ---- runout --------------------------------------------------------------

    def _wire_sensors(self):
        """The lanes' prep sensors as INDX's runout sensors ([indx_remap]
        runout_sensors: has the same effect): INDX hears their Klipper
        filament:runout / filament:insert events, and skips a partner whose
        spool is out. Only while INDX's spool join may run with the bridge."""
        r = self.remap
        if r is None or not hasattr(r, "sensors") or not hasattr(r, "dock_of"):
            return
        on = bool(self._setting("spool_join")) and self.enabled
        ok = None
        for d, lane in self.lanes.items():
            sensor = getattr(lane, "fila_prep", None)
            helper = getattr(sensor, "runout_helper", None)
            if helper is None:
                continue
            name = str(getattr(helper, "name", "") or "%s_prep" % lane.name)
            if ok is None:
                ok = _sends_events(helper)
            mine = self.wired.get(d) == name
            if on and ok:
                if r.sensors.get(d) is None or mine:
                    r.sensors[d] = helper
                    r.dock_of[name] = d
                    names = getattr(r, "sensor_names", None)
                    if isinstance(names, list):
                        while len(names) <= d:
                            names.append("")
                        names[d] = name
                    self.wired[d] = name
            elif mine:
                r.sensors.pop(d, None)
                r.dock_of.pop(name, None)
                names = getattr(r, "sensor_names", None)
                if isinstance(names, list) and d < len(names) and names[d] == name:
                    names[d] = ""
                self.wired.pop(d, None)
        if ok is not None:
            self.events_ok = ok
            if on and not ok:
                self._warn("this Klipper sends no filament:runout events (Kalico does): INDX's spool join cannot hear the lanes' sensors. "
                           "Only the lane in use reaches it, through AFC")

    def _ev_sensor_runout(self, eventtime, name):
        self.evt[name] = self.reactor.monotonic()

    def _join_ready(self):
        r = self.remap
        return (r is not None and bool(self._setting("spool_join")) and getattr(r, "state", "") == "ok"
                and getattr(r, "join_on", False) and hasattr(r, "_runout"))

    def _lane_runout(self, lane, orig, name):
        """AFC calls this for a runout on the lane in use while printing
        (prep or load sensor, or weight)."""
        d = self.lane_dock.get(lane.name)
        if d is not None:
            seen = self.reactor.monotonic() - self.evt.get(self.wired.get(d), -1e9) < 5.0
            if self.wired.get(d) and seen:
                # INDX heard the sensor itself: its spool join switches tools,
                # or pauses the print without a partner
                return
            if self._join_ready():
                self.gcode.respond_info("INDX-AFC: %s (T%d) ran out: INDX's spool join decides" % (lane.name, d))
                self.reactor.register_callback(lambda e, d=d: self._indx_runout(d))
                return
            self.gcode.respond_info("INDX-AFC: %s (T%d) ran out. Paused: INDX_AFC_UNLOAD LANE=%s, change the spool, INDX_AFC_LOAD LANE=%s, then RESUME."
                                    % (lane.name, d, lane.name, lane.name))
        if name == "_perform_infinite_runout":
            # AFC's infinite spool unloads into a lane on the same extruder;
            # on INDX the next lane feeds another tool: pause instead
            orig = getattr(type(lane), "_perform_pause_runout").__get__(lane)
        return orig()

    def _indx_runout(self, d):
        # From a reactor callback, outside the gcode lock: _runout runs PAUSE
        # or the join's gcode with run_script, which takes that lock
        try:
            self.remap._runout(d)
        except Exception:
            logging.exception("INDX-AFC: spool join for T%d", d)
            self.gcode.respond_info("INDX-AFC ! the spool join for T%d failed; pausing" % d)
            self._run_locked("PAUSE")

    # ---- print start ---------------------------------------------------------

    def _ev_before_prime(self, p):
        """INDX_PRINT_START, before anything heats: the lanes of this print."""
        self._wire_sensors()
        mode = str(self._setting("print_check") or "warn").lower()
        if mode == "off" or not self.enabled:
            return
        raw = str(p.get("DOCKS", "")).replace(" ", "")
        bad = []
        for part in [x for x in raw.split(",") if x != ""]:
            try:
                d = self._slicer_to_dock(int(float(part)))
            except ValueError:
                continue
            lane = self.lanes.get(d)
            if lane is None:
                continue
            if self._has_filament(d):
                if not lane.prep_state:
                    bad.append("T%d: %s's spool is out, about %.0f mm is left in its path" % (d, lane.name, lane.dist_hub))
            elif lane.load_state:
                bad.append("T%d is empty but %s has filament: INDX_AFC_LOAD LANE=%s" % (d, lane.name, lane.name))
        if not bad:
            return
        msg = "; ".join(bad)
        if mode == "stop":
            raise self.gcode.error("INDX-AFC: the print start stopped: " + msg)
        self.gcode.respond_info("INDX-AFC ! " + msg)
        self._notify(msg)

    def _notify(self, msg, title="Box Turtle lanes"):
        """A notice on the INDX page (INDX's _INDX_NOTE), from inside a command."""
        if self.printer.lookup_object("gcode_macro _INDX_NOTE", None) is None:
            return
        try:
            self.gcode.run_script_from_command('_INDX_NOTE LEVEL=warn TITLE="%s" MSG="%s"'
                                               % (title, msg.replace('"', "'").replace(";", ",")))
        except Exception:
            logging.exception("INDX-AFC: notify")

    # ---- M104 / M109 ---------------------------------------------------------

    def _cmd_temp(self, gcmd, wait):
        name = "M109" if wait else "M104"
        p = gcmd.get_command_parameters()
        raw = str(p.get("S", "0")).strip() or "0"
        try:
            float(raw)
        except ValueError:
            raise gcmd.error("%s: S%s is not a temperature" % (name, raw))
        cap = self._nozzle_max()
        if float(raw) > cap:
            # INDX_SET_TEMP does not check INDX's nozzle rating (its tool
            # change and LOAD_FILAMENT do). Nothing is heated. In a print a
            # G-code error would end it, so the print pauses instead.
            msg = "%s S%s is over the INDX nozzle's rating of %.0f C: nothing heated" % (name, raw, cap)
            if self._print_state() == "printing":
                self.gcode.respond_info("INDX-AFC ! %s. The print pauses: fix the temperature in the slicer, "
                                        "then RESUME or cancel." % msg)
                self._notify("%s. The print paused." % msg, title="Nozzle temperature")
                self.gcode.run_script_from_command("PAUSE")
                return
            raise gcmd.error("INDX-AFC: %s." % msg)
        args = "TARGET=%s WAIT=%d SRC=%s" % (raw, wait, name)
        if "T" in p:
            try:
                k = int(float(p["T"]))
            except ValueError:
                raise gcmd.error("%s: T%s is not a tool number" % (name, p["T"]))
            # the slicer's number: INDX_SET_TEMP with SRC=M104/M109 maps it
            # through INDX's tool map itself (and knows two slicer tools on
            # one dock), as INDX's own M104 does
            args += " TOOL=%d" % k
        self.gcode.run_script_from_command("INDX_SET_TEMP " + args)

    # ---- commands --------------------------------------------------------------

    def _lane_arg(self, gcmd):
        n = gcmd.get("LANE", None)
        if n is not None:
            if n not in self.lane_dock:
                raise gcmd.error("INDX-AFC: %s feeds no INDX tool; [indx_afc] lanes: %s" % (n, ", ".join(self.lane_dock)))
            return self.lane_dock[n]
        d = gcmd.get_int("TOOL", None)
        if d is None or d not in self.lanes:
            raise gcmd.error("INDX-AFC: give LANE= or TOOL= (tools with a lane: %s)" % ", ".join("T%d" % x for x in sorted(self.lanes)))
        return d

    def _order(self, docks):
        # the reference tool last, so it ends on the head (as INDX_LOAD_ALL)
        ref = self._vars().get("ref_tool", 0)
        return sorted(docks, key=lambda d: (d == ref, d))

    def cmd_LOAD(self, gcmd):
        self._load(self._lane_arg(gcmd), gcmd.get("TYPE", ""), gcmd.get("COLOR", ""))

    def cmd_UNLOAD(self, gcmd):
        self._unload(self._lane_arg(gcmd))

    def cmd_LOAD_ALL(self, gcmd):
        if self._print_state() == "printing" or self._paused():
            raise gcmd.error("INDX-AFC: no load of every lane during a print")
        typ = gcmd.get("TYPE", "")
        docks = [d for d, lane in self.lanes.items() if lane.load_state and not self._has_filament(d)]
        if not docks:
            gcmd.respond_info("INDX-AFC: nothing to load: every lane with filament already has it in its tool")
            return
        for d in docks:
            if not self._material(self.lanes[d], typ):
                raise gcmd.error("INDX-AFC: which material is in %s? Give TYPE= for the lanes without one in AFC. Nothing loaded." % self.lanes[d].name)
        for d in self._order(docks):
            self._load(d, typ)

    def cmd_CALIBRATE(self, gcmd):
        if not self.enabled:
            raise gcmd.error("INDX-AFC: no calibration, %s" % self.why_off)
        if self._print_state() == "printing" or self._paused():
            raise gcmd.error("INDX-AFC: no calibration during a print")
        maxd = gcmd.get_float("MAX", CAL_MAX, minval=100., maxval=10000.)
        save = gcmd.get_int("SAVE", 1, minval=0, maxval=1)
        if str(gcmd.get("LANE", "")).strip().upper() == "ALL":
            docks, skipped = [], []
            for d in sorted(self.lanes):
                lane = self.lanes[d]
                if self._has_filament(d):
                    skipped.append("%s (T%d has filament: unload it first)" % (lane.name, d))
                elif not lane.load_state:
                    skipped.append("%s (no filament at its load sensor)" % lane.name)
                else:
                    docks.append(d)
            if skipped:
                gcmd.respond_info("INDX-AFC: not calibrating " + "; ".join(skipped))
            if not docks:
                return
        else:
            docks = [self._lane_arg(gcmd)]
        lines = [self._calibrate(d, maxd, save) for d in self._order(docks)]
        gcmd.respond_info("INDX-AFC calibration:\n" + "\n".join(lines))

    def _calibrate(self, d, maxd, save):
        """Measures the lane's path to its tool's gears the way the ram finds
        them: the empty tool on the head, the lane homes toward the buffer's
        advance switch (fast, then back off and again slowly), the distance
        moved is the path plus what it takes to close the switch. The lane
        then goes back to its load sensor.

        dist_hub is saved shorter than measured, by the lane's
        homing_overshoot less CAL_SPARE (35 mm with AFC's 50). AFC's ram
        (load_then_home) first moves dist_hub - load_undershoot without
        watching the buffer, then homes for load_undershoot +
        homing_overshoot. Saved so, the blind part ends homing_overshoot -
        CAL_SPARE + load_undershoot (55 mm) before the point the switch
        closed, which leaves room for the filament the lane pushes after the
        tip meets the gears and before the switch closes; the homing still
        reaches CAL_SPARE past that point. A dist_hub longer than the path
        would drive the filament into the still gears at full speed."""
        lane = self.lanes[d]
        SM, AA, ST, MW = self.enums
        if lane.buffer_obj is None or getattr(lane.extruder_obj, "tool_start", None) != "buffer":
            raise self.gcode.error("INDX-AFC: %s cannot be calibrated: it needs its own buffer (buffer:) and pin_tool_start: buffer in [AFC_extruder]" % lane.name)
        if self._has_filament(d):
            raise self.gcode.error("INDX-AFC: T%d has filament (INDX records %s): INDX_AFC_UNLOAD LANE=%s first"
                                   % (d, self._vars().get("t%d_material" % d), lane.name))
        if not lane.load_state:
            raise self.gcode.error("INDX-AFC: %s has no filament at its load sensor: insert the spool first" % lane.name)
        if d in self.rammed:
            raise self.gcode.error("INDX-AFC: %s's filament is already at T%d's gears (an unfinished load): INDX_AFC_LOAD or INDX_AFC_UNLOAD it first"
                                   % (lane.name, d))
        if lane.get_toolhead_pre_sensor_state():
            raise self.gcode.error("INDX-AFC: %s's buffer shows its advance switch closed before anything moved. "
                                   "At rest it must be open (QUERY_BUFFER, check the wiring)" % lane.name)
        self._pickup_empty(d, "calibrating")
        self._apply(None)
        lane.unsync_to_extruder()
        lane.do_enable(True)
        endstop = lane.get_toolhead_endstop()
        old = lane.dist_hub
        self.gcode.respond_info("INDX-AFC: measuring %s to T%d's gears (up to %.0f mm, until its buffer triggers)" % (lane.name, d, maxd))
        try:
            # AFC's own calibration speed (its short-move speed): AFC never
            # homes to a buffer at its long-move speed
            cal_speed = getattr(SM, "CALIBRATION", SM.SHORT)
            _, fast, warn = lane.move_to(maxd, cal_speed, endstop=endstop, assist_active=AA.DYNAMIC, use_homing=True)
            if warn == MW.ERROR:
                # AFC's homing move itself failed (it logs why in AFC.log). Do
                # not try another homing move to bring it back: it would most
                # likely fail the same way
                raise self.gcode.error(
                    "INDX-AFC: AFC's homing move for %s failed, so the calibration stopped (AFC.log, near 'Homing start endstop=', says why). "
                    "The filament may be partly down the tube: run LANE_UNLOAD LANE=%s or pull it back to the load sensor by hand. "
                    "T%d is on the head, empty. dist_hub is unchanged." % (lane.name, lane.name, d))
            if not lane.get_toolhead_pre_sensor_state():
                self._cal_home(d, (fast or maxd) + CAL_BACKOFF, old)
                raise self.gcode.error("INDX-AFC: %s did not reach T%d's gears within %.0f mm (the buffer's advance switch never closed). "
                                       "The lane is back at its load sensor. Check the path, or give a larger MAX=." % (lane.name, d, maxd))
            # back off until the switch opens, then home again slowly
            back = 0.0
            while lane.get_toolhead_pre_sensor_state() and back < CAL_BACKOFF * CAL_BACKOFF_TRIES:
                lane.move_advanced(-CAL_BACKOFF, SM.SHORT)
                back += CAL_BACKOFF
            if lane.get_toolhead_pre_sensor_state():
                self._cal_home(d, fast, old)
                raise self.gcode.error("INDX-AFC: %s's advance switch stayed closed after backing off %.0f mm: check the buffer" % (lane.name, back))
            _, slow, warn = lane.move_to(back + CAL_BACKOFF, SM.SHORT, endstop=endstop, use_homing=True)
            if warn == MW.ERROR or not lane.get_toolhead_pre_sensor_state():
                self._cal_home(d, fast, old)
                raise self.gcode.error("INDX-AFC: %s's slow pass did not trigger the buffer again: check the buffer and try again" % lane.name)
        except Exception:
            lane.do_enable(False)
            raise
        measured = fast - back + slow
        short = max(float(getattr(lane, "homing_overshoot", None) or 50.0) - CAL_SPARE, 0.0)
        new = round(max(measured - short, 1.0), 1)
        self._cal_home(d, measured, new if save else old)
        line = "%s to T%d's gears: %.1f mm measured (fast pass %.1f, back %.0f, slow pass %.1f). dist_hub: %.1f (was %.1f)" % (
            lane.name, d, measured, fast, back, slow, new, old)
        if save:
            lane.dist_hub = new
            self.afc.function.ConfigRewrite(lane.fullname, "dist_hub", new, "\n" + line)
            line += ", saved"
        else:
            line += ", not saved (SAVE=0)"
        return line

    def _cal_home(self, d, distance, keep):
        """The lane back to its load sensor from `distance` mm out, then its
        dist_hub `keep`."""
        lane = self.lanes[d]
        lane.dist_hub = distance
        try:
            self._lane_home(d)
        finally:
            lane.dist_hub = keep

    def cmd_UNLOAD_ALL(self, gcmd):
        if self._print_state() == "printing" or self._paused():
            raise gcmd.error("INDX-AFC: no unload of every tool during a print")
        docks = [d for d in self.lanes if self._has_filament(d)]
        if not docks:
            gcmd.respond_info("INDX-AFC: nothing to unload")
            return
        for d in self._order(docks):
            self._unload(d)

    def cmd_LANE_HOME(self, gcmd):
        d = gcmd.get_int("TOOL")
        if d not in self.lanes or self.pulled != d:
            return
        if self._has_filament(d):
            self.pulled = None
            return
        self._lane_home(d)
        gcmd.respond_info("INDX-AFC: %s pulled back to its load sensor: nothing to pull out of T%d by hand" % (self.lanes[d].name, d))

    def cmd_SYNC(self, gcmd):
        if not self.ready:
            return
        self._install_commands()
        self._wire_sensors()
        self._reconcile()
        if gcmd.get_int("COLORS", 0):
            for d, lane in sorted(self.lanes.items()):
                col = self._color(lane, "")
                mat = self._vars().get("t%d_material" % d)
                have = str(self._vars().get("t%d_color" % d) or "").lstrip("#").lower()
                if col and mat not in EMPTY and col != have:
                    self.gcode.run_script_from_command("INDX_SET_MATERIAL TOOL=%d TYPE=%s COLOR=%s" % (d, mat, col))
                    gcmd.respond_info("INDX-AFC: T%d's color in INDX is now #%s (%s)" % (d, col, lane.name))
        if not gcmd.get_int("QUIET", 0):
            cur = self.afc.function.get_current_lane_obj()
            gcmd.respond_info("INDX-AFC: AFC's loaded lane is now %s" % (cur.name if cur else "none"))

    def _commands_state(self):
        h = getattr(self.gcode, "ready_gcode_handlers", {})
        bad = [c for c, fn in self.temp_fns.items() if h.get(c) is not fn]
        bad += [c for c in self.prev if h.get(c) is not self.mine.get(c)]
        err = getattr(self.afc, "error", None)
        if "AFC_PAUSE" in self.orig and getattr(err, "cmd_AFC_PAUSE", None) != self._afc_pause:
            bad.append("AFC's PAUSE")
        return bad

    def _join_text(self):
        r = self.remap
        if r is None:
            return "no [indx_remap] (INDX Suite): AFC pauses on a runout"
        if not self._setting("spool_join"):
            return "off (the Spool join setting): AFC pauses on a runout"
        if not getattr(r, "join_on", False):
            return "off in INDX (INDX_REMAP JOIN=1 turns it on): the print pauses on a runout"
        how = ("%d lane sensors given to INDX" % len(self.wired)) if self.wired else "through AFC for the lane in use"
        st = getattr(r, "state", "")
        return "INDX's, %s%s" % (how, "" if st == "ok" else "; it runs in prints started with INDX_PRINT_START")

    def cmd_STATUS(self, gcmd):
        act = self._active_tool()
        cur = self.afc.function.get_current_lane_obj() if self.afc else None
        lines = ["INDX-AFC bridge %s: %s" % (VERSION, "on" if self.enabled else "OFF, " + self.why_off),
                 "tool on the head: %s; AFC's loaded lane: %s" % (("T%d" % act) if act is not None and act >= 0 else "none", cur.name if cur else "none"),
                 "spool join: " + self._join_text()]
        for d in sorted(self.lanes):
            lane = self.lanes[d]
            synced = bool(getattr(lane, "_synced_to_extruder", False))
            lines.append("  T%d <- %s: INDX %s; lane %s, prep %s, load %s%s%s"
                         % (d, lane.name, self._vars().get("t%d_material" % d) or "empty",
                            getattr(lane, "status", "?"), "yes" if lane.prep_state else "no",
                            "yes" if lane.load_state else "no", ", SYNCED" if synced else "",
                            ", rammed to the gears" if d in self.rammed else ""))
        bad = self._commands_state() if self.ready else []
        if bad:
            lines.append("  ! not the bridge's any more: %s. Run INDX_AFC_SYNC." % ", ".join(bad))
        for w in self.warnings:
            lines.append("  ! " + w)
        gcmd.respond_info("\n".join(lines))

    def get_status(self, eventtime):
        cur = None
        try:
            cur = self.afc.function.get_current_lane_obj() if self.afc is not None else None
        except Exception:
            pass
        return {"version": VERSION, "enabled": self.enabled, "why_off": self.why_off,
                "lanes": {str(d): n for d, n in self.dock_lane.items()},
                "synced_lane": cur.name if cur is not None else None,
                "spool_join": bool(self.wired) or self._join_ready(),
                "sensors": {str(d): n for d, n in self.wired.items()},
                "rammed": sorted(self.rammed), "warnings": list(self.warnings)}


def _sends_events(helper):
    """Does this Klipper's runout helper send filament:runout (Kalico does,
    mainline Klipper does not)? AFC replaces the instance's
    note_filament_present; the class still has the firmware's."""
    try:
        return "filament:runout" in inspect.getsource(type(helper))
    except Exception:
        return False


def _afc_enums(lane):
    """AFC's SpeedMode, AssistActive, AFCLaneState and AFCMoveWarning, from
    the module the lane's own AFCLane class came from (however it was loaded)."""
    mods = []
    for cls in (type(lane).__mro__ if lane is not None else ()):
        m = sys.modules.get(getattr(cls, "__module__", ""))
        if m is not None and m not in mods:
            mods.append(m)
    for name in ("extras.AFC_lane", "plugins.AFC_lane", "AFC_lane"):
        if name in sys.modules and sys.modules[name] not in mods:
            mods.append(sys.modules[name])
    for m in mods:
        try:
            return (m.SpeedMode, m.AssistActive, m.AFCLaneState, m.AFCMoveWarning)
        except AttributeError:
            continue
    raise ImportError("[indx_afc] could not find AFC's SpeedMode / AssistActive / AFCLaneState / AFCMoveWarning (AFC_lane)")


def load_config(config):
    return IndxAfc(config)
