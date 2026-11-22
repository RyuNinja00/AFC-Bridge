#!/usr/bin/env python3
# Tests for klipper_extras/indx_afc.py against fake Klipper, AFC and INDX
# objects.
#   python3 tests/test_indx_afc.py
#   python3 tests/test_indx_afc.py ~/INDX_V1/klipper_extras/indx_remap.py
#       also runs the spool join tests against INDX's real indx_remap.py
#
# The fakes follow the interfaces the bridge uses (AFC main 5ea84ea / DEV
# 7324836, INDX v1.0-beta.5, Kalico 991e182):
#  - gcode: Kalico's lock. run_script takes the G-code lock, so inside a
#    command it never returns (here: Deadlock); run_script_from_command
#    outside the lock is refused. register_command wraps a handler, as Kalico
#    does for extended commands.
#  - INDX's macros as commands: LOAD_FILAMENT, INDX_UNLOAD and its
#    _INDX_UNLOAD_PULL, _TC_LATCH_MOVE (which unsyncs the lanes at Klipper
#    level, as indx_api does for a tool manager), the events.
#  - AFC: lanes, units, function, error (PAUSE/RESUME), T<n> registration.
# They do not prove the printer behaves; they check the bridge calls the
# right things in the right order and never waits on its own lock.

import os
import sys
import types
from enum import Enum

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "klipper_extras"))


class SpeedMode(Enum):
    NONE = None
    LONG = 1
    SHORT = 2
    HUB = 3
    NIGHT = 4
    CALIBRATION = 5


class AssistActive(Enum):
    YES = 1
    NO = 2
    DYNAMIC = 3


class AFCLaneState(str, Enum):
    NONE = "None"
    LOADED = "Loaded"
    TOOLED = "Tooled"


class AFCMoveWarning(Enum):
    NONE = 0
    WARN = 1
    ERROR = 2


# under a name the bridge does not guess: it must find the enums through the
# lane's own class
afc_mod = types.ModuleType("somewhere.AFC_lane_mod")
afc_mod.SpeedMode, afc_mod.AssistActive, afc_mod.AFCLaneState, afc_mod.AFCMoveWarning = SpeedMode, AssistActive, AFCLaneState, AFCMoveWarning
sys.modules["somewhere.AFC_lane_mod"] = afc_mod

import indx_afc  # noqa: E402

LOG = []
NODEF = object()


class GError(Exception):
    pass


class Deadlock(BaseException):
    """run_script inside a command: on the printer this never returns."""


class Reactor:
    NEVER = 9e15

    def __init__(self):
        self.cbs = []
        self.timers = []
        self.now = 100.0

    def monotonic(self):
        return self.now

    def register_callback(self, cb, waketime=None):
        self.cbs.append(cb)

    def register_timer(self, cb, waketime=None):
        self.timers.append(cb)

    def run(self):
        n = 0
        while self.cbs:
            n += 1
            assert n < 200, "callbacks never settle"
            self.cbs.pop(0)(self.now)

    def tick(self):
        for t in list(self.timers):
            t(self.now)
        self.run()


class Gcmd:
    error = GError

    def __init__(self, gcode, line):
        self.gcode, self.line = gcode, line
        w = line.split()
        self.name = w[0].upper()
        self.raw = line[len(w[0]):].strip()
        if len(self.name) > 1 and self.name[0] in "MG" and self.name[1:].isdigit():
            self.params = {x[0].upper(): x[1:] for x in w[1:]}
        else:
            self.params = {x.split("=", 1)[0].upper(): x.split("=", 1)[1].strip('"') for x in w[1:] if "=" in x}

    def get_command_parameters(self):
        return dict(self.params)

    def get_raw_command_parameters(self):
        return self.raw

    def get(self, k, default=NODEF, **kw):
        if k in self.params:
            return self.params[k]
        if default is NODEF:
            raise GError("missing %s" % k)
        return default

    def get_int(self, k, default=NODEF, **kw):
        v = self.get(k, default)
        return v if v is None or v is default else int(v)

    def get_float(self, k, default=NODEF, **kw):
        v = self.get(k, default)
        return v if v is None or v is default else float(v)

    def respond_info(self, msg):
        self.gcode.respond_info(msg)


class Gcode:
    error = GError

    def __init__(self, printer):
        self.p = printer
        self.ready_gcode_handlers = {}
        self.info = []
        self.locked = 0

    def register_command(self, name, fn, desc=None):
        if fn is None:
            return self.ready_gcode_handlers.pop(name, None)
        if name in self.ready_gcode_handlers:
            raise Exception("gcode command %s already registered" % name)
        if name[0] in "MG" and name[1:].isdigit():
            self.ready_gcode_handlers[name] = fn
        else:
            self.ready_gcode_handlers[name] = lambda gcmd, f=fn: f(gcmd)     # Kalico's extended-params wrapper

    def respond_info(self, msg):
        self.info.append(msg)
        LOG.append("say " + msg)

    def _lines(self, script):
        for line in [l.strip() for l in script.split("\n") if l.strip()]:
            LOG.append("run " + line)
            g = Gcmd(self, line)
            h = self.ready_gcode_handlers.get(g.name)
            if h is not None:
                h(g)

    def run_script(self, script):
        if self.locked:
            LOG.append("DEADLOCK " + script)
            raise Deadlock(script)
        self.locked += 1
        try:
            self._lines(script)
        finally:
            self.locked -= 1

    def run_script_from_command(self, script):
        if not self.locked:
            raise GError("run_script_from_command requires the G-code mutex")
        self._lines(script)

    def cmd(self, line):
        """A command from the console or the file."""
        self.run_script(line)


class Heater:
    def __init__(self):
        self.target = 0.0

    def get_status(self, t):
        return {"target": self.target}


class Extruder:
    def __init__(self):
        self.heater = Heater()

    def get_heater(self):
        return self.heater


class Toolhead:
    def __init__(self):
        self.ext = Extruder()

    def wait_moves(self):
        pass

    def get_extruder(self):
        return self.ext

    def get_status(self, t):
        return {"max_velocity": 300.0, "homed_axes": "xyz"}


class GcodeMove:
    def __init__(self):
        self.pos = [10.0, 20.0, 5.0, 0.0]

    def get_status(self, t):
        return {"gcode_position": list(self.pos), "absolute_coordinates": True, "absolute_extrude": False, "speed": 50.0}


class SaveVars:
    def __init__(self):
        self.allVariables = {"ref_tool": 0}


class Stats:
    def __init__(self):
        self.state = "standby"

    def get_status(self, t):
        return {"state": self.state, "filename": ""}


class PauseResume:
    def __init__(self):
        self.is_paused = False

    def get_status(self, t):
        return {"is_paused": self.is_paused}


class Macro:
    def __init__(self, **v):
        self.variables = v

    def get_status(self, t):
        return dict(self.variables)


class Printer:
    config_error = Exception
    state_message = "Printer is ready"

    def __init__(self):
        self.reactor = Reactor()
        self.gcode = Gcode(self)
        self.handlers = {}
        self.objects = {"gcode": self.gcode, "toolhead": Toolhead(), "save_variables": SaveVars(), "print_stats": Stats(),
                        "pause_resume": PauseResume(), "gcode_move": GcodeMove()}

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def lookup_objects(self, module=None):
        return [(n, o) for n, o in self.objects.items() if module is None or n == module or n.startswith(module + " ")]

    def register_event_handler(self, e, fn):
        self.handlers.setdefault(e, []).append(fn)

    def send_event(self, e, *a):
        for fn in list(self.handlers.get(e, [])):
            fn(*a)


class Config:
    def __init__(self, printer, opts):
        self.p, self.o = printer, opts
        self.error = Exception

    def get_printer(self):
        return self.p

    def getlist(self, k):
        return [x for x in self.o[k].split(",")]

    def getfloat(self, k, d=None, minval=None, maxval=None):
        return float(self.o.get(k, d))

    def getboolean(self, k, d=None):
        return bool(self.o.get(k, d))

    def get(self, k, d=None):
        return self.o.get(k, d)


# ---- AFC fakes ------------------------------------------------------------

class Ext:
    def __init__(self):
        self.name = "extruder"
        self.tool_start = "buffer"
        self.lane_loaded = None


class Unit:
    unload_on_runout = False

    def __init__(self):
        self.ram_ok = True

    def load_then_home(self, lane, dist, assist, endstop):
        LOG.append("ram %s %.0f %s kl_synced=%s" % (lane.name, dist, endstop, lane.kl_synced))
        if lane.path is not None:
            # AFC's load_then_home: dist - load_undershoot (20) without watching
            # the buffer, then a homing move for the rest
            blind = dist - 20.0
            lane.blind_past_gears = max(0.0, lane.tip + blind - lane.path)
            lane.tip += blind
            return lane.move_to(20.0, SpeedMode.SHORT, endstop=endstop, use_homing=True)
        lane.rammed = self.ram_ok
        return (self.ram_ok, dist, AFCMoveWarning.NONE if self.ram_ok else AFCMoveWarning.ERROR)

    def lane_tool_unloaded(self, lane):
        pass


class RunoutHelper:
    """Kalico's: note_filament_present sends filament:runout / filament:insert."""
    def __init__(self, printer, name):
        self.printer, self.name = printer, name
        self.filament_present, self.sensor_enabled, self.runout_pause = True, True, False
        self.mute = False

    def note_filament_present(self, eventtime, is_filament_present=None):
        if is_filament_present == self.filament_present:
            return
        self.filament_present = is_filament_present
        if self.mute:
            return                      # inside Kalico's event delay
        self.printer.send_event("filament:insert" if is_filament_present else "filament:runout", eventtime, self.name)


class Sensor:
    def __init__(self, printer, name):
        self.runout_helper = RunoutHelper(printer, name)


class BaseLane:
    pass


BaseLane.__module__ = "somewhere.AFC_lane_mod"


class Lane(BaseLane):
    def __init__(self, afc, printer, name, map_, ext):
        self.afc, self.printer, self.name, self.map, self.extruder_obj = afc, printer, name, map_, ext
        self.hub, self.buffer_obj, self.runout_lane = "direct", object(), None
        self.unit_obj = Unit()
        self.prep_state = self.load_state = self.raw_load_state = True
        self.dist_hub, self.load_es, self.short_move_dis = 900.0, "load", 10.0
        self.status, self.tool_loaded = AFCLaneState.LOADED, False
        self._synced_to_extruder = False      # AFC's flag
        self.kl_synced = False                # the stepper on the extruder's trapq (Klipper level)
        self.enabled = False
        self.rammed = False
        self.material, self.color = "PLA", "#FF0000"
        self.fila_prep = Sensor(printer, name + "_prep")
        self.loaded_to_hub = False
        self.net = 0.0
        self.fullname = "AFC_stepper " + name
        # a physical model for INDX_AFC_CALIBRATE (None: the old, simple behaviour)
        self.path = None          # mm from the load sensor to the tool's gears
        self.tip = 0.0            # mm the filament tip is past the load sensor
        self.comp = 6.0           # mm more the lane pushes before the advance switch closes
        self.adv_at_rest = False  # a buffer wired wrong: advance closed before anything moves
        self.blind_past_gears = 0.0  # mm the ram's blind part pushed past the gears
        self.homing_fails = False    # AFC's homing move raises (an MCU or endstop problem)
        self.speeds = []             # speed modes of move_to calls

    def prep_switch(self, state):
        """The prep switch changes: AFC's handle_prep_runout."""
        self.prep_state = state
        self.fila_prep.runout_helper.note_filament_present(self.printer.reactor.monotonic(), state)
        if (not state and self.afc.function.get_current_lane_obj() is self and self.afc.function.is_printing()
                and self.raw_load_state):
            if self.runout_lane is not None:
                self._perform_infinite_runout()
            else:
                self._perform_pause_runout()

    def _perform_pause_runout(self):
        LOG.append("afc pause_runout %s" % self.name)

    def _perform_infinite_runout(self):
        LOG.append("afc infinite_runout %s" % self.name)

    def sync_to_extruder(self, update_current=True):
        self._synced_to_extruder = self.kl_synced = True

    def unsync_to_extruder(self, update_current=True):
        self._synced_to_extruder = self.kl_synced = False

    def set_tool_loaded(self, normal_toolchange=False):
        self.tool_loaded, self.status = True, AFCLaneState.TOOLED
        self.extruder_obj.lane_loaded = self.name

    def set_tool_unloaded(self, normal_toolchange=False):
        self.tool_loaded, self.status = False, AFCLaneState.NONE
        self.extruder_obj.lane_loaded = None

    def do_enable(self, on):
        self.enabled = on

    def get_toolhead_endstop(self):
        return "buffer"

    def get_toolhead_pre_sensor_state(self):
        if self.path is not None:
            return self.adv_at_rest or self.tip >= self.path + self.comp
        return self.rammed

    def move_to(self, distance, speed_mode, endstop=None, assist_active=None, use_homing=True):
        LOG.append("lane_move %s %.0f %s kl_synced=%s" % (self.name, distance, endstop, self.kl_synced))
        self.speeds.append(speed_mode)
        if self.path is not None and use_homing and self.homing_fails:
            return (False, 0.0, AFCMoveWarning.ERROR)
        if self.path is not None and use_homing:
            total = abs(distance) + 50.0                         # AFC's homing_overshoot
            if distance > 0 and endstop == "buffer":
                trig = self.path + self.comp - self.tip
                if trig <= total:
                    moved = trig + (3.0 if speed_mode != SpeedMode.SHORT else 0.2)   # stops just past the trigger
                    self.tip += moved
                    return (True, moved, AFCMoveWarning.NONE)
                self.tip += total
                return (False, total, AFCMoveWarning.NONE)        # AFC: no trigger is not an error
            if distance < 0 and endstop == "load":
                if self.tip <= total:
                    moved, self.tip = self.tip, 0.0
                    return (True, moved, AFCMoveWarning.NONE)
                self.tip -= total
                return (False, total, AFCMoveWarning.ERROR)
        return (True, abs(distance), AFCMoveWarning.NONE)

    def move_advanced(self, distance, speed_mode, assist_active=None):
        self.net += distance
        if self.path is not None:
            self.tip += distance
        LOG.append("lane_adv %s %.3f kl_synced=%s" % (self.name, distance, self.kl_synced))


class Function:
    def __init__(self, afc):
        self.afc = afc

    def get_current_lane_obj(self):
        n = self.afc.ext.lane_loaded
        return self.afc.lanes.get(n) if n else None

    def ConfigRewrite(self, section, key, value, msg=""):
        LOG.append("config_rewrite [%s] %s: %s" % (section, key, value))

    def handle_activate_extruder(self):
        cur = self.get_current_lane_obj()
        for l in self.afc.lanes.values():
            if l is not cur:
                l.do_enable(False)
                l.unsync_to_extruder()
        if cur is not None:
            cur.do_enable(True)
            cur.sync_to_extruder()

    def is_printing(self, check_movement=False):
        return self.afc.printer.objects["print_stats"].state == "printing"


class Err:
    AFC_RENAME_PAUSE_NAME = "_AFC_RENAMED_PAUSE_"
    AFC_RENAME_RESUME_NAME = "_AFC_RENAMED_RESUME_"

    def __init__(self):
        self.reset = 0

    def AFC_error(self, msg, pause=True):
        LOG.append("afc_error " + msg)

    def cmd_AFC_PAUSE(self, gcmd):
        LOG.append("AFC PAUSE moves (z hop)")

    def cmd_AFC_RESUME(self, gcmd):
        LOG.append("AFC RESUME moves (restore_pos)")

    def reset_failure(self):
        self.reset += 1


class AFC:
    homing_enabled = True
    home_to_tool = True
    in_toolchange = False

    def __init__(self, printer):
        self.printer = printer
        self.ext = Ext()
        self.function = Function(self)
        self.error = Err()
        self.lanes = {}
        for i in range(4):
            n = "lane%d" % (i + 1)
            self.lanes[n] = Lane(self, printer, n, "T%d" % i, self.ext)
        printer.register_event_handler("klippy:connect", self._connect)

    def _connect(self):
        g = self.printer.gcode
        g.register_command("CHANGE_TOOL", self.cmd_CHANGE_TOOL)
        for lane in self.lanes.values():          # TcmdAssign: T<n> for a lane with map:
            g.register_command(lane.map, lambda gcmd, l=lane: self.CHANGE_TOOL(l))

    def ready(self):
        # AFC's klippy:ready, here after the bridge's: AFC takes M104/M109
        g = self.printer.gcode
        for c in ("M104", "M109"):
            g.register_command(c, None)
            g.register_command(c, lambda gcmd, c=c: LOG.append("AFC %s heats the shared extruder" % c))

    def prep(self):
        """AFC's PREP, 2 s after ready: PAUSE and RESUME renamed to AFC's
        (by the error object's attributes at that moment)."""
        g = self.printer.gcode
        for name, new, attr in (("PAUSE", self.error.AFC_RENAME_PAUSE_NAME, "cmd_AFC_PAUSE"),
                                ("RESUME", self.error.AFC_RENAME_RESUME_NAME, "cmd_AFC_RESUME")):
            prev = g.register_command(name, None)
            g.register_command(new, prev)
            g.register_command(name, getattr(self.error, attr))

    def cmd_CHANGE_TOOL(self, gcmd):
        lane = gcmd.get("LANE", None)
        if lane is None:
            raise GError("AFC CHANGE_TOOL: LANE= needed")
        self.CHANGE_TOOL(self.lanes[lane])

    def save_vars(self):
        pass

    def CHANGE_TOOL(self, cur_lane, purge_length=None, restore_pos=True, new_extruder_temp=None):
        LOG.append("AFC CHANGE_TOOL ran")

    def TOOL_LOAD(self, cur_lane, purge_length=None, set_start_time=False):
        LOG.append("AFC TOOL_LOAD ran")

    def TOOL_UNLOAD(self, cur_lane, set_start_time=True, force_unload=False):
        LOG.append("AFC TOOL_UNLOAD ran")

    def LANE_UNLOAD(self, cur_lane, gcmd=None):     # AFC DEV's signature
        LOG.append("AFC LANE_UNLOAD %s%s" % (cur_lane.name, " gcmd" if gcmd is not None else ""))


# ---- INDX fakes -----------------------------------------------------------

class Manager:
    name = "indx_afc"
    owns_tool_commands = False


class Api:
    def __init__(self, printer):
        self.p = printer
        self.manager = Manager()
        self.extra = {}             # more indx_api status fields for a test (nozzle_max_temp, no_heat_tools)

    def get_status(self, t):
        sv = self.p.objects["save_variables"].allVariables
        act = sv.get("active_tool", -1)
        # beta.6: a tool kept on the head without proof (seat_suspect) is "attention"
        tools = [{"state": "attention" if sv.get("seat_suspect", -1) == n else ("on_head" if n == act else "docked")}
                 for n in range(4)]
        st = {"active_tool": act, "tool_count": 4, "tools": tools}
        st.update(self.extra)
        return st


class Remap:
    """indx_remap as beta.5 has it, as far as the bridge touches it:
    including its run_script calls inside commands."""
    def __init__(self, printer):
        self.printer, self.gcode, self.reactor = printer, printer.gcode, printer.reactor
        self.state, self.join_on, self.map, self.ran_out = "", True, {}, []
        self.sensors, self.dock_of, self.sensor_names = {}, {}, []
        self.join, self.join_next, self.join_busy, self.joins = {}, {}, None, []
        self.partner = {}
        self.change_busy = False
        self.last_busy = False
        printer.register_event_handler("klippy:ready", self._ready)
        printer.register_event_handler("filament:runout", self._ev_runout)
        printer.register_event_handler("filament:insert", self._ev_insert)
        self.gcode.register_command("INDX_RUNOUT", self.cmd_RUNOUT)
        self.gcode.register_command("_INDX_REMAP_JOINED", self.cmd_JOINED)
        self.gcode.register_command("INDX_REMAP_PLAN", self.cmd_PLAN)

    def _ready(self):
        self.reactor.register_timer(self._watch)

    def _manager(self):
        return self.printer.objects["indx_api"].manager

    def _print_state(self):
        return self.printer.objects["print_stats"].state

    def _active_tool(self):
        return self.printer.objects["save_variables"].allVariables.get("active_tool", -1)

    def cmd_PLAN(self, gcmd):
        self.map, self.ran_out = {}, []
        self.state = "off" if self._manager() is not None else "ok"

    def slicer_to_dock(self, k):
        return self.map.get(k, k) if self.state == "ok" and self._print_state() in ("printing", "paused") else k

    def _ev_runout(self, eventtime, name):
        d = self.dock_of.get(name)
        if d is not None:
            self.reactor.register_callback(lambda e: self._runout(d))

    def _ev_insert(self, eventtime, name):
        d = self.dock_of.get(name)
        if d is not None and d in self.ran_out:
            self.ran_out.remove(d)

    def cmd_RUNOUT(self, gcmd):
        d = gcmd.get_int("DOCK")
        if gcmd.get_int("PRESENT", 0):
            if d in self.ran_out:
                self.ran_out.remove(d)
            return
        self._runout(d, gcmd)

    def _runout(self, d, gcmd=None):
        st = self._print_state()
        if st not in ("printing", "paused"):
            return
        if d not in self.ran_out:
            self.ran_out.append(d)
        to = self.partner.get(d) if (self.join_on and self.state == "ok") else None
        if to is None:
            if st == "printing":
                self.gcode.run_script("PAUSE")
            return
        act = self._active_tool()
        if act == d and st == "printing" and not self.change_busy:
            self._start_join(d, to)
        elif act == d and st == "printing":
            self.join_busy = (d, to)
        else:
            self.map[d] = to

    def _start_join(self, d, to):
        if d in self.ran_out and self._active_tool() == d and self._print_state() == "printing":
            self.map[d] = to
            rec = {"from": d, "to": to}
            if self.join:
                self.join_next = rec
                return
            self.join = rec
            self.gcode.run_script("_INDX_SPOOL_JOIN")

    def cmd_JOINED(self, gcmd):
        nxt = self.join_next
        self.join, self.join_next = {}, {}
        if nxt:
            self.join = nxt
            self.gcode.run_script("_INDX_SPOOL_JOIN")

    def _watch(self, eventtime):
        busy = self.change_busy
        if self.last_busy and not busy and self.join_busy is not None:
            frm, to = self.join_busy
            self.join_busy = None
            self.map[frm] = to
        self.last_busy = busy
        return eventtime + 0.5


class Indx:
    """INDX's macros as commands."""
    def __init__(self, printer):
        self.p = printer
        g = printer.gcode
        for name in ("_INDX_CHANGE_TOOL", "INDX_CHANGE_TOOL", "LOAD_FILAMENT", "INDX_UNLOAD", "_INDX_UNLOAD_PULL", "_TC_LATCH_MOVE",
                     "INDX_SET_TEMP", "PAUSE", "RESUME", "_INDX_SPOOL_JOIN", "INDX_PRINT_START", "INDX_SET_MATERIAL", "INDX_PRIME_TEST"):
            g.register_command(name, getattr(self, "c_" + name.lstrip("_").lower()))
        printer.objects["gcode_macro LOAD_FILAMENT"] = Macro(filament_presets={"PLA": {}, "PETG": {}, "ABS": {}, "ASA": {}, "PA": {}, "PC": {}, "TPU": {}})
        printer.objects["gcode_macro INDX_UNLOAD"] = Macro(unload_length=65, unload_speed=15)
        printer.objects["gcode_macro _INDX_CONSTANTS"] = Macro(run_current=0.6)
        printer.objects["gcode_macro INDX_PRIME_TEST"] = Macro()
        printer.objects["gcode_macro TOOL_POSITIONS"] = Macro(tool_count=4, no_heat_tools=[])
        printer.objects["gcode_macro _TOOLCHANGE_VARS"] = Macro(change_busy=0)
        printer.objects["gcode_macro _INDX_AFC"] = Macro(spool_join=True, print_check="warn", load_type="none", prime_test=False,
                                                         ram_relief=0.0, unload_chunk=8.0)

    @property
    def sv(self):
        return self.p.objects["save_variables"].allVariables

    def lanes(self):
        return list(self.p.objects["AFC"].lanes.values())

    def following(self):
        return ",".join(l.name for l in self.lanes() if l.kl_synced) or "none"

    def ev(self, e, **kw):
        self.p.send_event("indx:" + e, {k.upper(): str(v) for k, v in kw.items()})

    def latch(self, what):
        """A latch move with a tool manager: every synced stepper off the
        extruder (Klipper level; AFC's flag stays), the move, back on."""
        on = [l for l in self.lanes() if l.kl_synced]
        for l in on:
            l.kl_synced = False
        LOG.append("%s following=%s" % (what, self.following()))
        for l in on:
            l.kl_synced = True

    def change(self, t):
        act = self.sv.get("active_tool", -1)
        if act == t:
            return
        if act >= 0:
            self.latch("latch move")
            self.sv["active_tool"] = -1
            self.ev("after_park", tool=act)
        self.latch("latch move")
        self.sv["active_tool"] = t
        self.ev("after_pickup", tool=t)

    def c_indx_change_tool(self, g):
        self.change(g.get_int("TOOL"))

    def set_active(self, t):
        # _INDX_SET_ACTIVE_TOOL: every write of active_tool fires tool_state
        self.sv["active_tool"] = t
        self.ev("tool_state", tool=t)

    def by_hand(self, what, t=-1):
        if what == "seat":            # INDX_SEAT_TOOL_BY_HAND TOOL=t
            self.latch("latch move")
            self.sv["seat_suspect"] = -1
            self.set_active(t)
        elif what == "release":       # INDX_RELEASE_TOOL_BY_HAND
            self.latch("latch move")
            self.sv["seat_suspect"] = -1
            self.set_active(-1)
        elif what == "forget":        # INDX_FORGET_TOOL
            self.sv["seat_suspect"] = -1
            self.set_active(-1)
        elif what == "stopped_park":  # beta.6: "T<n> may still be on the head", the stop
            act = self.sv.get("active_tool", -1)
            self.latch("latch move")
            self.sv["seat_suspect"] = act
            self.set_active(act)
            self.ev("stop", reason="T%d may still be on the head" % act, tool=act)

    def c_indx_set_temp(self, g):
        pass

    def c_indx_set_material(self, g):
        self.sv["t%d_color" % g.get_int("TOOL")] = g.get("COLOR")

    def c_indx_prime_test(self, g):
        pass

    def c_load_filament(self, g):
        t = g.get_int("TOOL")
        self.change(t)
        LOG.append("feed T%d following=%s" % (t, self.following()))
        self.sv["t%d_material" % t] = g.get("TYPE")
        self.ev("filament_state", tool=t, loaded=1)

    def c_indx_unload(self, g):
        t = g.get_int("TOOL")
        self.change(t)
        LOG.append("tip T%d following=%s" % (t, self.following()))
        self.p.gcode.run_script_from_command("_INDX_UNLOAD_PULL TOOL=%d" % t)
        self.sv["t%d_material" % t] = "NONE"
        self.ev("filament_state", tool=t, loaded=0)

    def c_indx_unload_pull(self, g):
        # stock: one 65 mm latch move
        self.p.gcode.run_script_from_command("_TC_LATCH_MOVE E=-65 F=900 I=0.6")

    def c_tc_latch_move(self, g):
        self.latch("latch E=%.3f" % g.get_float("E"))

    def c_pause(self, g):
        LOG.append("INDX PAUSE")
        self.p.objects["pause_resume"].is_paused = True
        self.p.objects["print_stats"].state = "paused"
        self.p.objects["gcode_move"].pos = [300.0, 300.0, 15.0, 0.0]     # the pause park

    def c_resume(self, g):
        LOG.append("INDX RESUME")
        self.p.objects["pause_resume"].is_paused = False
        self.p.objects["print_stats"].state = "printing"

    def c_indx_spool_join(self, g):
        r = self.p.objects["indx_remap"]
        LOG.append("spool join T%s -> T%s" % (r.join.get("from"), r.join.get("to")))
        self.change(r.join["to"])
        self.p.gcode.run_script_from_command("_INDX_REMAP_JOINED")

    def c_indx_print_start(self, g):
        self.p.objects["print_stats"].state = "printing"
        self.p.gcode.run_script_from_command("INDX_REMAP_PLAN " + g.get_raw_command_parameters())
        self.ev("before_prime", docks=g.get("TOOLS", ""), first=0)


FAILS = []


def check(name, ok, info=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  -> %s" % (info,)))
    if not ok:
        FAILS.append(name)


def setup(manager=True, opts=None, pre=None, remap_cls=Remap):
    del LOG[:]
    p = Printer()
    afc = AFC(p)
    api = Api(p)
    if not manager:
        api.manager = None
    p.objects.update({"AFC": afc, "indx_api": api})
    indx = Indx(p)
    p.indx = indx
    remap = remap_cls(p)
    p.objects["indx_remap"] = remap
    if pre:
        pre(p, afc)
    o = {"lanes": "lane1, lane2, lane3, lane4"}
    o.update(opts or {})
    b = indx_afc.IndxAfc(Config(p, o))
    p.objects["indx_afc"] = b
    p.send_event("klippy:connect")
    p.send_event("klippy:ready")
    afc.ready()                 # AFC's ready after the bridge's: the worst order
    p.reactor.run()             # the bridge's late checks
    afc.prep()
    p.reactor.run()
    del LOG[:]
    return p, afc, api, remap, b


def synced(afc):
    return [l.name for l in afc.lanes.values() if l._synced_to_extruder]


def err(fn):
    try:
        fn()
        return None
    except GError as e:
        return str(e)


def no_deadlock():
    return not any(l.startswith("DEADLOCK") for l in LOG)


def run_all():
    # ---- startup ----
    p, afc, api, remap, b = setup()
    g = p.gcode
    check("ready: on, with INDX's manager registration", b.enabled)
    check("ready: a correct AFC config has no warnings", not b.warnings, b.warnings)
    check("ready: AFC's enums found through the lane's class", b.enums[0] is SpeedMode)
    check("ready: AFC's methods redirected", afc.CHANGE_TOOL == b._afc_change_tool and afc.TOOL_LOAD == b._afc_tool_load)
    check("ready: M104/M109 are the bridge's again after AFC took them at its ready",
          g.ready_gcode_handlers["M104"] is b.temp_fns["M104"] and g.ready_gcode_handlers["M109"] is b.temp_fns["M109"])
    check("ready: the bridge's commands all in place", b._commands_state() == [], b._commands_state())
    prev = dict(b.prev)
    g.cmd("INDX_AFC_SYNC")
    g.cmd("INDX_AFC_SYNC")
    check("ready: checking again does not wrap a command twice", b.prev == prev)
    g.register_command("M104", None)
    g.register_command("M104", lambda gc: None)
    check("status: a command taken by someone else is reported", b._commands_state() == ["M104"], b._commands_state())
    g.cmd("INDX_AFC_SYNC")
    check("sync: takes it back", b._commands_state() == [])
    g.cmd("INDX_AFC_STATUS")
    check("status prints", any("INDX-AFC bridge 0.2.8: on" in m for m in g.info), g.info[-1:])
    check("get_status for macros and the page", b.get_status(0)["enabled"] and b.get_status(0)["sensors"]["0"] == "lane1_prep")

    def bad(p2, afc2):
        afc2.lanes["lane2"].hub = "Turtle_1"
        afc2.lanes["lane3"].map = "T7"
        afc2.ext.tool_start = "PA1"
        afc2.home_to_tool = False
        p2.objects["AFC_prep"] = types.SimpleNamespace(enable=True)
    p2, afc2, _, _, b2 = setup(pre=bad)
    w = b2.warnings
    check("ready: a shared hub is flagged", any("lane2: set hub: direct" in x for x in w), w)
    check("ready: a lane T number not matching its dock is flagged", any("lane3: set map: T2" in x for x in w), w)
    check("ready: no ram sensor is flagged", any("pin_tool_start: buffer" in x for x in w), w)
    check("ready: home_to_tool off is flagged", any("home_to_tool" in x for x in w), w)
    check("ready: AFC_prep's lane test at every start is flagged", any("AFC_prep" in x for x in w), w)

    # ---- sync follows the tool on the head ----
    p, afc, api, remap, b = setup()
    g, sv = p.gcode, p.objects["save_variables"].allVariables
    sv.update({"t0_material": "PLA", "t1_material": "PETG", "t2_material": "NONE", "t3_material": "PLA"})
    g.cmd("_INDX_CHANGE_TOOL TOOL=1")
    check("pickup T1 (loaded): only lane2 synced, and it is AFC's loaded lane",
          synced(afc) == ["lane2"] and afc.ext.lane_loaded == "lane2", synced(afc))
    g.cmd("_INDX_CHANGE_TOOL TOOL=3")
    check("change T1 -> T3: only lane4 synced", synced(afc) == ["lane4"], synced(afc))
    check("the parked lane counts as loaded for AFC, not empty", afc.lanes["lane2"].status == AFCLaneState.LOADED)
    g.cmd("_INDX_CHANGE_TOOL TOOL=2")
    check("pickup an empty tool (INDX records NONE): nothing synced", synced(afc) == [] and afc.ext.lane_loaded is None, synced(afc))
    lm = [l for l in LOG if l.startswith("latch move")]
    check("no lane follows any latch move", len(lm) == 5 and all(l.endswith("following=none") for l in lm), lm)

    # ---- beta.6: the tool on the head changed outside a tool change ----
    p, afc, api, remap, b = setup()
    g, sv = p.gcode, p.objects["save_variables"].allVariables
    sv.update({"t0_material": "PLA", "t1_material": "PETG", "t2_material": "NONE", "t3_material": "PLA"})
    g.cmd("_INDX_CHANGE_TOOL TOOL=1")
    p.indx.by_hand("release")
    check("released by hand (INDX_RELEASE_TOOL_BY_HAND): no lane synced, AFC has none loaded",
          synced(afc) == [] and afc.ext.lane_loaded is None, (synced(afc), afc.ext.lane_loaded))
    p.indx.by_hand("seat", 3)
    check("seated by hand (INDX_SEAT_TOOL_BY_HAND TOOL=3, loaded): lane4 synced",
          synced(afc) == ["lane4"] and afc.ext.lane_loaded == "lane4", (synced(afc), afc.ext.lane_loaded))
    p.indx.by_hand("forget")
    check("forgotten (INDX_FORGET_TOOL): no lane synced", synced(afc) == [], synced(afc))
    p.indx.by_hand("seat", 2)
    check("seated by hand, an empty tool: no lane synced", synced(afc) == [], synced(afc))
    g.cmd("_INDX_CHANGE_TOOL TOOL=0")
    p.indx.by_hand("stopped_park")
    check("stopped park (beta.6, T0 kept on the head, not verified): lane1 unsynced, it may be in its dock",
          synced(afc) == [] and afc.ext.lane_loaded is None, (synced(afc), afc.ext.lane_loaded))
    g.cmd("INDX_AFC_SYNC")
    check("stopped park: INDX_AFC_SYNC does not sync the suspect tool's lane either", synced(afc) == [], synced(afc))
    p.indx.by_hand("seat", 0)
    check("stopped park, then INDX_SEAT_TOOL_BY_HAND TOOL=0 LOCKED=1: lane1 synced again", synced(afc) == ["lane1"], synced(afc))
    g.cmd("_INDX_CHANGE_TOOL TOOL=3")
    p.indx.by_hand("stopped_park")
    p.indx.by_hand("forget")
    g.cmd("_INDX_CHANGE_TOOL TOOL=1")
    check("stopped park, INDX_FORGET_TOOL, then a pickup of T1: lane2 synced", synced(afc) == ["lane2"], synced(afc))
    lm = [l for l in LOG if l.startswith("latch move")]
    check("by hand too: no lane follows a latch move", lm and all(l.endswith("following=none") for l in lm), lm)

    p, afc, api, remap, b = setup(manager=False)
    p.objects["save_variables"].allVariables["t1_material"] = "PLA"
    p.gcode.cmd("_INDX_CHANGE_TOOL TOOL=1")
    check("INDX has no manager: bridge off, no lane ever synced", not b.enabled and synced(afc) == [], synced(afc))
    check("INDX has no manager: warned", any("NOT be synced" in w for w in b.warnings), b.warnings)
    r = err(lambda: p.gcode.cmd("INDX_AFC_LOAD LANE=lane1"))
    check("INDX has no manager: loads refused", r and "tool manager" in r, r)

    def stale(p2, afc2):
        afc2.ext.lane_loaded = "lane1"
        afc2.lanes["lane1"]._synced_to_extruder = afc2.lanes["lane1"].kl_synced = True
        p2.objects["save_variables"].allVariables.update({"active_tool": 2, "t2_material": "ASA"})
    del LOG[:]
    p, afc, api, remap, b = setup(pre=stale)
    check("restart: AFC said lane1, INDX has T2 loaded on the head: lane3 is AFC's now and synced",
          afc.ext.lane_loaded == "lane3" and synced(afc) == ["lane3"], (afc.ext.lane_loaded, synced(afc)))

    # ---- tool changes ----
    p, afc, api, remap, b = setup()
    g, sv = p.gcode, p.objects["save_variables"].allVariables
    g.cmd("T2")
    check("AFC's T2 -> INDX's change to T2; AFC's own CHANGE_TOOL never runs",
          "run _INDX_CHANGE_TOOL TOOL=2 KEEP_F=1" in LOG and "AFC CHANGE_TOOL ran" not in LOG and sv["active_tool"] == 2, LOG)
    del LOG[:]
    g.cmd("CHANGE_TOOL TOOL=1")
    check("INDX's CHANGE_TOOL TOOL=1 still works (AFC owns the name)", sv["active_tool"] == 1, LOG)
    check("CHANGE_TOOL TOOL=1 asks INDX to put the print's feedrate back after the change (KEEP_F=1, beta.11)",
          "run _INDX_CHANGE_TOOL TOOL=1 KEEP_F=1" in LOG, LOG)
    del LOG[:]
    g.cmd("CHANGE_TOOL TOOL=2 KEEP_F=0")
    check("CHANGE_TOOL ... KEEP_F=0: INDX's own KEEP_F=1 comes last and wins, as in INDX's CHANGE_TOOL",
          "run _INDX_CHANGE_TOOL TOOL=2 KEEP_F=0 KEEP_F=1" in LOG, LOG)
    g.cmd("CHANGE_TOOL LANE=lane4")
    check("AFC's CHANGE_TOOL LANE=lane4 -> T3", sv["active_tool"] == 3, LOG)
    del LOG[:]
    g.locked += 1
    afc.CHANGE_TOOL(afc.lanes["lane1"], new_extruder_temp=230)
    g.locked -= 1
    check("AFC's change with a temperature passes TEMP=", "run _INDX_CHANGE_TOOL TOOL=0 TEMP=230 KEEP_F=1" in LOG, LOG)

    # ---- M104 / M109 ----
    del LOG[:]
    g.cmd("M104 S220 T1")
    g.cmd("M109 S200")
    check("M104 T1 S220 -> INDX_SET_TEMP with the slicer's T (INDX maps it)",
          "run INDX_SET_TEMP TARGET=220 WAIT=0 SRC=M104 TOOL=1" in LOG, LOG)
    check("M109 S200 -> INDX_SET_TEMP WAIT=1", "run INDX_SET_TEMP TARGET=200 WAIT=1 SRC=M109" in LOG, LOG)
    check("AFC's M104 never heats", not any("heats the shared extruder" in l for l in LOG), LOG)

    # nozzle rating (INDX_SET_TEMP does not check it): 300 C unless INDX says otherwise
    del LOG[:]
    try:
        g.cmd("M104 S310")
        emsg = None
    except GError as e:
        emsg = str(e)
    check("M104 S310, not printing: refused with an error, nothing heated (INDX's default rating 300 C)",
          emsg is not None and "300 C" in emsg and not any("INDX_SET_TEMP" in l for l in LOG), (emsg, LOG))
    del LOG[:]
    g.cmd("M104 S300")
    check("M104 S300: at the rating, passed on", "run INDX_SET_TEMP TARGET=300 WAIT=0 SRC=M104" in LOG, LOG)
    api.extra["nozzle_max_temp"] = 280.0
    del LOG[:]
    p.objects["print_stats"].state = "printing"
    g.cmd("M109 S290 T0")
    check("M109 S290 in a print, INDX rating 280 C: no error, nothing heated, the print pauses",
          not any("INDX_SET_TEMP" in l for l in LOG) and "INDX PAUSE" in LOG, LOG)
    p.objects["print_stats"].state = "standby"
    api.extra.clear()
    del LOG[:]
    g.cmd("M104 S0")
    check("M104 S0 always passes", "run INDX_SET_TEMP TARGET=0 WAIT=0 SRC=M104" in LOG, LOG)

    # ---- PAUSE / RESUME ----
    del LOG[:]
    p.objects["print_stats"].state = "printing"
    g.cmd("PAUSE")
    check("PAUSE: INDX's runs, AFC's z hop does not", "INDX PAUSE" in LOG and not any("AFC PAUSE moves" in l for l in LOG), LOG)
    afc.in_toolchange = True
    g.cmd("RESUME")
    check("RESUME: INDX's runs, AFC's position restore does not; AFC's failure state is cleared",
          "INDX RESUME" in LOG and not any("AFC RESUME moves" in l for l in LOG) and afc.error.reset == 1 and not afc.in_toolchange, LOG)

    # ---- INDX_AFC_CALIBRATE: the lane's path to its tool's gears ----
    p, afc, api, remap, b = setup()
    g, sv = p.gcode, p.objects["save_variables"].allVariables
    for n in afc.lanes.values():
        n.path = 2000.0
    l1 = afc.lanes["lane1"]
    del LOG[:]
    g.cmd("INDX_AFC_CALIBRATE LANE=lane1")
    # fast pass stops at 2006 + 3; back off 20; slow pass from 1989 to 2006 + 0.2
    check("calibrate lane1 (path 2000 mm, 6 mm to close advance): measured 2006.2, dist_hub saved 35 mm shorter (overshoot 50 - 15): 1971.2",
          abs(l1.dist_hub - 1971.2) < 0.01 and "config_rewrite [AFC_stepper lane1] dist_hub: 1971.2" in LOG, (l1.dist_hub, LOG))
    check("calibrate: the empty T0 picked up first, the lane unsynced the whole time",
          sv["active_tool"] == 0 and all("kl_synced=False" in l for l in LOG if l.startswith("lane_")), LOG)
    check("calibrate: the lane ends back at its load sensor, nothing rammed",
          l1.tip == 0.0 and 0 not in b.rammed and synced(afc) == [], (l1.tip, b.rammed, synced(afc)))
    check("calibrate: the result is reported", any("2006.2 mm measured (fast pass 2009.0, back 20, slow pass 17.2)" in l and "saved" in l for l in LOG), LOG)
    del LOG[:]
    g.cmd("INDX_AFC_LOAD LANE=lane1 TYPE=PLA")
    check("the next load rams with the calibrated dist_hub; its blind part stops short of the gears",
          any(l.startswith("ram lane1 1971") for l in LOG) and l1.blind_past_gears == 0.0 and "say INDX-AFC: lane1 loaded into T0" in LOG, (LOG, l1.blind_past_gears))
    # with 50 mm of compression before the switch closes, the blind part still stops short
    for comp, name in ((50.0, "lane3"),):
        lc = afc.lanes[name]
        lc.comp = comp
        g.cmd("INDX_AFC_CALIBRATE LANE=%s" % name)
        g.cmd("INDX_AFC_LOAD LANE=%s TYPE=PLA" % name)
        check("50 mm of compression before the switch closes: the calibrated ram's blind part still stops short of the gears, and it homes",
              lc.blind_past_gears == 0.0 and "say INDX-AFC: lane3 loaded into T2" in LOG, (lc.dist_hub, lc.blind_past_gears, LOG[-3:]))
    # why not longer: AFC rams dist_hub - 20 mm without watching the buffer
    l2x = afc.lanes["lane2"]
    l2x.dist_hub = 2100.0
    g.cmd("INDX_AFC_LOAD LANE=lane2 TYPE=PLA")
    check("(model check) a dist_hub 100 mm too long drives the filament 80 mm past the gears before AFC homes",
          abs(l2x.blind_past_gears - 80.0) < 0.01, l2x.blind_past_gears)

    p, afc, api, remap, b = setup()
    for n in afc.lanes.values():
        n.path = 1500.0
    l2 = afc.lanes["lane2"]
    g = p.gcode
    g.cmd("INDX_AFC_CALIBRATE LANE=lane2 SAVE=0")
    check("SAVE=0: measured and reported, dist_hub unchanged, nothing saved",
          l2.dist_hub == 900.0 and not any(l.startswith("config_rewrite") for l in LOG)
          and any("1506.2 mm measured" in l and "not saved" in l for l in LOG), (l2.dist_hub, LOG))
    check("SAVE=0: the lane back at its load sensor", l2.tip == 0.0, l2.tip)
    try:
        g.cmd("INDX_AFC_CALIBRATE LANE=lane2 MAX=1000")
        emsg = None
    except GError as e:
        emsg = str(e)
    check("path longer than MAX=1000: an error, the lane back at its load sensor, dist_hub unchanged",
          emsg is not None and "within 1000 mm" in emsg and l2.tip == 0.0 and l2.dist_hub == 900.0, (emsg, l2.tip, l2.dist_hub))

    l2.homing_fails = True
    del LOG[:]
    try:
        g.cmd("INDX_AFC_CALIBRATE LANE=lane2")
        emsg = None
    except GError as e:
        emsg = str(e)
    check("AFC's homing move fails: stops with the reason and what to do, no second homing move, dist_hub unchanged, lane motor off",
          emsg is not None and "AFC.log" in emsg and "LANE_UNLOAD LANE=lane2" in emsg
          and len([l for l in LOG if l.startswith("lane_move")]) == 1 and l2.dist_hub == 900.0 and not l2.enabled, (emsg, LOG))
    check("calibration homes at AFC's calibration speed, not its long-move speed",
          l2.speeds[-1] == SpeedMode.CALIBRATION, l2.speeds)
    l2.homing_fails = False

    p, afc, api, remap, b = setup()
    g, sv = p.gcode, p.objects["save_variables"].allVariables
    for n in afc.lanes.values():
        n.path = 1200.0
    sv["t1_material"] = "PLA"
    afc.lanes["lane3"].adv_at_rest = True
    del LOG[:]
    try:
        g.cmd("INDX_AFC_CALIBRATE LANE=lane2")
        emsg = None
    except GError as e:
        emsg = str(e)
    check("calibrate a tool that has filament: refused, nothing moved",
          emsg is not None and "unload" in emsg.lower() and not any(l.startswith("lane_") for l in LOG), (emsg, LOG))
    try:
        g.cmd("INDX_AFC_CALIBRATE LANE=lane3")
        emsg = None
    except GError as e:
        emsg = str(e)
    check("calibrate with the advance switch closed at rest: refused before any move",
          emsg is not None and "advance switch closed" in emsg and not any(l.startswith("lane_") for l in LOG), (emsg, LOG))
    afc.lanes["lane3"].adv_at_rest = False
    afc.lanes["lane4"].load_state = False
    del LOG[:]
    g.cmd("INDX_AFC_CALIBRATE LANE=ALL")
    check("LANE=ALL: calibrates the empty tools with filament, skips T1 (filament) and lane4 (no filament)",
          sorted(l for l in LOG if l.startswith("config_rewrite")) == ["config_rewrite [AFC_stepper lane1] dist_hub: 1171.2",
                                                                         "config_rewrite [AFC_stepper lane3] dist_hub: 1171.2"]
          and any("not calibrating lane2" in l and "lane4" in l for l in LOG), LOG)
    p.objects["print_stats"].state = "printing"
    try:
        g.cmd("INDX_AFC_CALIBRATE LANE=lane1")
        emsg = None
    except GError as e:
        emsg = str(e)
    check("calibrate during a print: refused", emsg is not None and "during a print" in emsg, emsg)
    p.objects["print_stats"].state = "standby"

    # ---- a lane feeding a tool without a hotend ----
    def no_heat(p, afc):
        p.objects["indx_api"].extra["no_heat_tools"] = [2]
    try:
        setup(pre=no_heat)
        emsg = None
    except Exception as e:
        emsg = str(e)
    check("start: a lane feeding a tool in INDX's no_heat_tools is a config error naming the lane and the tool",
          emsg is not None and "lane3" in emsg and "T2" in emsg and "no_heat_tools" in emsg, emsg)

    # ---- load ----
    p, afc, api, remap, b = setup()
    g, sv = p.gcode, p.objects["save_variables"].allVariables
    p.objects["gcode_macro _INDX_AFC"].variables["ram_relief"] = 1.5
    sv.update({"t0_material": "PLA"})
    g.cmd("_INDX_CHANGE_TOOL TOOL=0")
    del LOG[:]
    g.cmd("INDX_AFC_LOAD LANE=lane2")
    order = [l for l in LOG if l.startswith(("run INDX_CHANGE_TOOL", "ram ", "lane_adv", "feed ", "run LOAD_FILAMENT"))]
    check("load: LOAD_FILAMENT with AFC's material and color; pick up, ram with nothing synced, relief, then the feed with the lane following",
          order == ["run LOAD_FILAMENT TOOL=1 TYPE=PLA COLOR=ff0000", "run INDX_CHANGE_TOOL TOOL=1 RETURN=none",
                    "ram lane2 900 buffer kl_synced=False", "lane_adv lane2 -1.500 kl_synced=False", "feed T1 following=lane2"], order)
    check("load: lane2 synced after, lane1 not", synced(afc) == ["lane2"], synced(afc))
    check("load: no waiting on the G-code lock", no_deadlock())

    del LOG[:]
    g.cmd("LOAD_FILAMENT TOOL=2 TYPE=PETG")
    order = [l for l in LOG if l.startswith(("ram ", "feed "))]
    check("INDX's own LOAD_FILAMENT (its page, INDX_LOAD, INDX_LOAD_ALL): the lane rams and follows too",
          order == ["ram lane3 900 buffer kl_synced=False", "feed T2 following=lane3"] and sv["t2_material"] == "PETG", order)
    del LOG[:]
    g.cmd("LOAD_FILAMENT TOOL=2 TYPE=PETG")
    check("LOAD_FILAMENT on a loaded tool (a reload): no second ram, the lane follows",
          not any(l.startswith("ram ") for l in LOG) and "feed T2 following=lane3" in LOG, LOG)
    afc.lanes["lane4"].load_state = False
    del LOG[:]
    g.cmd("LOAD_FILAMENT TOOL=3 TYPE=PLA")
    check("LOAD_FILAMENT with no filament in the lane: INDX's load by hand, said so",
          not any(l.startswith("ram ") for l in LOG) and any("by hand" in l for l in LOG), LOG)

    p, afc, api, remap, b = setup(opts={"lanes": "lane1, -, lane3, lane4"})
    p.gcode.cmd("LOAD_FILAMENT TOOL=1 TYPE=PLA")
    check("a dock no lane feeds loads as plain INDX", "feed T1 following=none" in LOG and not any(l.startswith("ram") for l in LOG), LOG)

    p, afc, api, remap, b = setup()
    g, sv = p.gcode, p.objects["save_variables"].allVariables
    sv["t1_material"] = "PETG"
    r = err(lambda: g.cmd("INDX_AFC_LOAD TOOL=1"))
    check("load: refused when INDX has filament in the tool", r and "already has filament" in r, r)
    afc.lanes["lane3"].load_state = False
    r = err(lambda: g.cmd("INDX_AFC_LOAD TOOL=2"))
    check("load: refused with no filament at the lane's load sensor", r and "load sensor" in r, r)
    afc.lanes["lane4"].unit_obj.ram_ok = False
    del LOG[:]
    r = err(lambda: g.cmd("INDX_AFC_LOAD TOOL=3"))
    check("load: a ram that never reaches the gears stops before any feed, nothing left synced",
          r and "did not reach" in r and not any(l.startswith("feed") for l in LOG) and synced(afc) == [], (r, LOG))
    afc.lanes["lane1"].material = "Silk Rainbow"
    r = err(lambda: g.cmd("INDX_AFC_LOAD TOOL=0"))
    check("load: an unknown material asks for TYPE=", r and "TYPE=" in r, r)
    p.objects["gcode_macro _INDX_AFC"].variables["load_type"] = "PETG"
    del LOG[:]
    g.cmd("INDX_AFC_LOAD TOOL=0")
    check("load: the Load material setting fills in", any(l.startswith("run LOAD_FILAMENT TOOL=0 TYPE=PETG") for l in LOG), LOG)
    check("load: AFC's PETG-CF is INDX's PETG preset; PA-CF is PA; PLA+ is PLA; PCTG is not PC",
          b._material(types.SimpleNamespace(material="PETG-CF"), "") == "PETG" and b._material(types.SimpleNamespace(material="PA-CF"), "") == "PA"
          and b._material(types.SimpleNamespace(material="PLA+"), "") == "PLA"
          and b._material(types.SimpleNamespace(material="PCTG"), "") == "PETG")       # falls to the Load material setting
    p.objects["print_stats"].state = "printing"
    r = err(lambda: g.cmd("INDX_AFC_LOAD TOOL=3 TYPE=PLA"))
    check("load: refused while printing", r and "while printing" in r, r)
    r = err(lambda: g.cmd("INDX_AFC_LOAD_ALL"))
    check("load all: refused during a print", r and "during a print" in r, r)

    # ---- load all / unload all ----
    p, afc, api, remap, b = setup()
    g, sv = p.gcode, p.objects["save_variables"].allVariables
    sv.update({"t1_material": "PLA", "ref_tool": 0})
    afc.lanes["lane4"].load_state = False
    g.cmd("INDX_AFC_LOAD_ALL")
    feeds = [l for l in LOG if l.startswith("feed")]
    check("load all: the lanes with filament and an empty tool, the reference tool last",
          feeds == ["feed T2 following=lane3", "feed T0 following=lane1"] and sv["active_tool"] == 0, feeds)
    p.objects["gcode_macro _INDX_AFC"].variables["prime_test"] = True
    sv["t1_material"] = "NONE"
    del LOG[:]
    g.cmd("INDX_AFC_LOAD TOOL=1")
    check("load: the prime test setting runs INDX_PRIME_TEST after",
          "run INDX_PRIME_TEST" in LOG and LOG.index("run INDX_PRIME_TEST") > LOG.index("feed T1 following=lane2"), LOG)
    del LOG[:]
    g.cmd("INDX_AFC_UNLOAD_ALL")
    homes = [l for l in LOG if l.startswith("lane_move")]
    check("unload all: every loaded tool, each lane home", len(homes) == 3 and all(sv["t%d_material" % d] == "NONE" for d in (0, 1, 2)), homes)

    # ---- unload ----
    p, afc, api, remap, b = setup()
    g, sv = p.gcode, p.objects["save_variables"].allVariables
    sv.update({"t2_material": "PLA"})
    g.cmd("INDX_AFC_UNLOAD LANE=lane3")
    p.reactor.run()
    seq = [l for l in LOG if l.startswith(("tip ", "latch E", "lane_adv", "lane_move"))]
    latch = [l for l in seq if l.startswith("latch E")]
    adv = [l for l in seq if l.startswith("lane_adv")]
    check("unload: the tip shaping has the lane following", seq[0] == "tip T2 following=lane3", seq[:1])
    check("unload: the 65 mm pull runs in parts of 8 mm at most, the lane taking each back right after",
          len(latch) == 9 and len(adv) == 9 and all(seq[1 + 2 * i].startswith("latch E=-7.222") and seq[2 + 2 * i].startswith("lane_adv") for i in range(9))
          and abs(afc.lanes["lane3"].net + 65.0) < 0.01, seq)
    check("unload: the lane's own moves run unsynced", all("kl_synced=False" in l for l in adv), adv)
    check("unload: then the lane back to its load sensor, once", [l for l in seq if l.startswith("lane_move")] ==
          ["lane_move lane3 -900 load kl_synced=False"] and seq[-1].startswith("lane_move"), seq)
    check("unload: nothing synced after, AFC has no loaded lane", synced(afc) == [] and afc.ext.lane_loaded is None)
    check("unload: no waiting on the G-code lock", no_deadlock())

    sv.update({"t0_material": "PLA"})
    del LOG[:]
    g.cmd("INDX_UNLOAD TOOL=0 CONFIRM=1")
    p.reactor.run()
    seq = [l for l in LOG if l.startswith(("latch E", "lane_adv", "lane_move"))]
    check("INDX's own unload (its page): the lane follows the pull and goes home after it, once",
          len([l for l in seq if l.startswith("lane_adv")]) == 9 and seq[-1] == "lane_move lane1 -900 load kl_synced=False"
          and LOG.count("lane_move lane1 -900 load kl_synced=False") == 1 and no_deadlock(), seq)
    p.objects["gcode_macro _INDX_AFC"].variables["unload_chunk"] = 20.0
    sv.update({"t0_material": "PLA"})
    del LOG[:]
    g.cmd("INDX_AFC_UNLOAD TOOL=0")
    check("unload: the Unload step setting changes the parts (20 mm: 4)", len([l for l in LOG if l.startswith("latch E")]) == 4, LOG)

    p, afc, api, remap, b = setup(opts={"lanes": "lane1, -, lane3, lane4"})
    p.objects["save_variables"].allVariables["t1_material"] = "PLA"
    p.gcode.cmd("INDX_UNLOAD TOOL=1 CONFIRM=1")
    check("a dock no lane feeds unloads as plain INDX (one 65 mm pull)", [l for l in LOG if l.startswith("latch E")] == ["latch E=-65.000 following=none"], LOG)

    p, afc, api, remap, b = setup()
    g = p.gcode
    p.objects["save_variables"].allVariables.update({"t0_material": "PLA"})
    g.locked += 1
    afc.LANE_UNLOAD(afc.lanes["lane1"])
    check("eject: refused while the tool still has the filament", any("still has" in l for l in LOG) and "AFC LANE_UNLOAD lane1" not in LOG, LOG)
    afc.LANE_UNLOAD(afc.lanes["lane2"])
    check("eject: an empty tool's lane ejects as AFC does", "AFC LANE_UNLOAD lane2" in LOG, LOG)
    # AFC DEV's cmd_LANE_UNLOAD calls self.LANE_UNLOAD(cur_lane, gcmd), and
    # passes gcmd= (and load_to_gears=, restore_temp=) to the others
    del LOG[:]
    afc.LANE_UNLOAD(afc.lanes["lane2"], object())
    check("eject from AFC DEV (gcmd passed by position): no TypeError, gcmd handed on", "AFC LANE_UNLOAD lane2 gcmd" in LOG, LOG)
    for call in (lambda: afc.TOOL_LOAD(afc.lanes["lane9"] if "lane9" in afc.lanes else Lane(afc, p, "lane9", "T9", afc.ext), 0, False, False, True, object()),
                 lambda: afc.TOOL_UNLOAD(Lane(afc, p, "lane9", "T9", afc.ext), True, False, True, object())):
        try:
            call()
            emsg = None
        except TypeError as e:
            emsg = str(e)
        check("AFC DEV's extra TOOL_LOAD/TOOL_UNLOAD arguments by position: no TypeError", emsg is None, emsg)
    del LOG[:]
    afc.TOOL_LOAD(afc.lanes["lane2"])
    afc.TOOL_UNLOAD(afc.lanes["lane2"])
    g.locked -= 1
    check("AFC's Load and Unload buttons run the bridge's, never AFC's own",
          "feed T1 following=lane2" in LOG and any(l.startswith("lane_move lane2") for l in LOG)
          and "AFC TOOL_LOAD ran" not in LOG and "AFC TOOL_UNLOAD ran" not in LOG, LOG)

    # ---- a paused print ----
    p, afc, api, remap, b = setup()
    g, sv = p.gcode, p.objects["save_variables"].allVariables
    sv.update({"t0_material": "PLA", "t1_material": "PLA"})
    g.cmd("_INDX_CHANGE_TOOL TOOL=0")
    p.objects["print_stats"].state = "printing"
    p.objects["toolhead"].ext.heater.target = 215.0
    g.cmd("PAUSE")
    r = err(lambda: g.cmd("INDX_AFC_UNLOAD TOOL=1"))
    check("paused: a parked tool cannot be unloaded", r and "only that tool" in r, r)
    del LOG[:]
    g.cmd("INDX_AFC_UNLOAD TOOL=0")
    p.objects["toolhead"].ext.heater.target = 0.0          # INDX's unload turns the heater off
    check("paused: the tool on the head unloads, then the head goes back to the pause position",
          sv["t0_material"] == "NONE" and "run G1 X300.000 Y300.000 F9000" in LOG, [l for l in LOG if l.startswith("run G1")])
    del LOG[:]
    g.cmd("INDX_AFC_LOAD TOOL=0")
    runs = [l for l in LOG if l.startswith("run ")]
    check("paused: the new spool loads without tip shaping (TIP=0), back to the pause position, the print's temperature again",
          "run LOAD_FILAMENT TOOL=0 TYPE=PLA COLOR=ff0000 TIP=0" in runs and "run G1 X300.000 Y300.000 F9000" in runs
          and runs[-1] == "run INDX_SET_TEMP TARGET=215.0 WAIT=0 SRC=INDX_AFC" and synced(afc) == ["lane1"], runs)
    g.cmd("RESUME")
    check("paused: RESUME goes on, no waiting on the lock anywhere", p.objects["print_stats"].state == "printing" and no_deadlock())

    # ---- print start check ----
    p, afc, api, remap, b = setup()
    g, sv = p.gcode, p.objects["save_variables"].allVariables
    sv.update({"t0_material": "PLA", "t1_material": "NONE"})
    afc.lanes["lane1"].prep_state = False
    g.cmd("INDX_PRINT_START TOOLS=0,1,2")
    says = [l for l in LOG if l.startswith("say INDX-AFC !")]
    check("print start: a spool that is out and an empty tool with a ready lane are named",
          len(says) == 1 and "T0: lane1's spool is out" in says[0] and "T1 is empty but lane2" in says[0] and "T2 is empty but lane3" in says[0], says)
    p.objects["print_stats"].state = "standby"
    p.objects["gcode_macro _INDX_AFC"].variables["print_check"] = "stop"
    r = err(lambda: g.cmd("INDX_PRINT_START TOOLS=0"))
    check("print start: with the check on stop, it stops the start", r and "print start stopped" in r, r)
    p.objects["print_stats"].state = "standby"
    p.objects["gcode_macro _INDX_AFC"].variables["print_check"] = "off"
    del LOG[:]
    g.cmd("INDX_PRINT_START TOOLS=0")
    check("print start: off says nothing", not any("INDX-AFC !" in l for l in LOG), LOG)

    # ---- colors ----
    p, afc, api, remap, b = setup()
    g, sv = p.gcode, p.objects["save_variables"].allVariables
    sv.update({"t0_material": "PLA", "t0_color": "00ff00", "t1_material": "NONE"})
    g.cmd("INDX_AFC_SYNC COLORS=1")
    check("sync COLORS=1: AFC's color into INDX for loaded tools only",
          "run INDX_SET_MATERIAL TOOL=0 TYPE=PLA COLOR=ff0000" in LOG and not any("INDX_SET_MATERIAL TOOL=1" in l for l in LOG), LOG)


def spool_join(remap_cls, tag):
    fake = remap_cls is Remap

    def start(p, remap, tools="0,1", partner=None, others="PLA"):
        sv = p.objects["save_variables"].allVariables
        for d in range(4):
            sv.update({"t%d_material" % d: "PLA" if d == 0 else others, "t%d_color" % d: "ff0000", "t%d_nozzle" % d: 0.4})
        p.gcode.cmd("_INDX_CHANGE_TOOL TOOL=0")
        p.gcode.cmd("INDX_PRINT_START TOOLS=%s" % tools)
        p.reactor.run()
        if fake:
            remap.partner = partner or {}
        del LOG[:]

    p, afc, api, remap, b = setup(remap_cls=remap_cls)
    g = p.gcode
    sv = p.objects["save_variables"].allVariables
    check(tag + "join: INDX is told this manager only feeds the tools", remap._manager() is None)
    check(tag + "join: the lanes' prep sensors are INDX's runout sensors",
          remap.dock_of == {"lane%d_prep" % (d + 1): d for d in range(4)} and remap.sensors[2] is afc.lanes["lane3"].fila_prep.runout_helper
          and b.events_ok, remap.dock_of)
    start(p, remap, partner={0: 1})
    check(tag + "join: INDX's plan runs with the bridge as tool manager (state ok)", remap.state == "ok", remap.state)
    afc.lanes["lane1"].prep_switch(False)
    p.reactor.run()
    check(tag + "runout on the printing tool: INDX's spool join switches to the partner, once",
          [l for l in LOG if l.startswith("spool join")] == ["spool join T0 -> T1"] and sv["active_tool"] == 1, LOG)
    check(tag + "runout: AFC does not pause, and nothing waits on the G-code lock",
          "afc pause_runout lane1" not in LOG and "INDX PAUSE" not in LOG and no_deadlock(), LOG)
    check(tag + "runout: the partner's lane is synced after the join", synced(afc) == ["lane2"], synced(afc))
    check(tag + "runout: the slicer's T0 now goes to T1", remap.slicer_to_dock(0) == 1)
    del LOG[:]
    g.cmd("T0")
    check(tag + "after the join: AFC's T0 from the file stays on T1", "run _INDX_CHANGE_TOOL TOOL=1 KEEP_F=1" in LOG, LOG)
    afc.lanes["lane1"].prep_switch(True)
    check(tag + "a new spool in the lane clears INDX's ran-out mark", 0 not in remap.ran_out, remap.ran_out)

    p, afc, api, remap, b = setup(remap_cls=remap_cls)
    start(p, remap, others="PETG")
    afc.lanes["lane1"].prep_switch(False)
    p.reactor.run()
    check(tag + "runout without a partner: the print pauses once (INDX's), AFC's pause stays out",
          LOG.count("INDX PAUSE") == 1 and "afc pause_runout lane1" not in LOG and no_deadlock(), LOG)

    p, afc, api, remap, b = setup(remap_cls=remap_cls)
    start(p, remap, partner={0: 1})
    afc.lanes["lane1"].fila_prep.runout_helper.mute = True
    afc.lanes["lane1"].prep_switch(False)
    p.reactor.run()
    check(tag + "runout with no Klipper event (inside its event delay): AFC's detection goes to INDX's join",
          [l for l in LOG if l.startswith("spool join")] == ["spool join T0 -> T1"] and "afc pause_runout lane1" not in LOG and no_deadlock(), LOG)

    p, afc, api, remap, b = setup(remap_cls=remap_cls)
    start(p, remap, partner={0: 1})
    afc.lanes["lane2"].prep_switch(False)          # the partner's own spool is out (parked)
    p.reactor.run()
    del LOG[:]
    afc.lanes["lane1"].prep_switch(False)
    p.reactor.run()
    if fake:
        check(tag + "(the fake has no partner rules: skipped)", True)
    else:
        check(tag + "a partner whose own spool is out is not chosen: the next one is",
              [l for l in LOG if l.startswith("spool join")] == ["spool join T0 -> T2"], LOG)

    p, afc, api, remap, b = setup(remap_cls=remap_cls)
    start(p, remap, partner={0: 1})
    p.objects["gcode_macro _INDX_AFC"].variables["spool_join"] = False
    g = p.gcode
    g.cmd("INDX_AFC_SYNC")
    check(tag + "setting off: the sensors are taken back from INDX, the manager gate is INDX's again",
          remap.dock_of == {} and remap._manager() is not None, remap.dock_of)
    afc.lanes["lane1"].runout_lane = "lane2"
    del LOG[:]
    afc.lanes["lane1"].prep_switch(False)
    p.reactor.run()
    check(tag + "setting off, AFC infinite spool set: AFC pauses (never its lane swap into one extruder)",
          "afc pause_runout lane1" in LOG and "afc infinite_runout lane1" not in LOG and not any(l.startswith("spool join") for l in LOG), LOG)

    p, afc, api, remap, b = setup(remap_cls=remap_cls)
    start(p, remap, partner={0: 1})
    g = p.gcode
    g.cmd("INDX_RUNOUT DOCK=0")
    p.reactor.run()
    check(tag + "INDX_RUNOUT DOCK=0: the join runs after the command, no waiting on the lock (stock beta.5 waits forever here)",
          [l for l in LOG if l.startswith("spool join")] == ["spool join T0 -> T1"] and no_deadlock(), LOG)
    g.cmd("INDX_RUNOUT DOCK=0 PRESENT=1")
    check(tag + "INDX_RUNOUT PRESENT=1 is INDX's own", 0 not in remap.ran_out)

    # beta.5: a second runout while a join is on its way back
    p, afc, api, remap, b = setup(remap_cls=remap_cls)
    start(p, remap, "0,1,3", partner={0: 1, 1: 3})
    g = p.gcode
    sv = p.objects["save_variables"].allVariables
    orig = p.indx.c_indx_spool_join
    state = {"done": False}

    def join_with_second_runout(gc):
        if remap.join.get("to") == 1 and not state["done"]:
            state["done"] = True
            LOG.append("spool join T0 -> T1")
            p.indx.change(1)
            if 2 not in remap.ran_out:
                remap.ran_out.append(2)          # (real partner rules: T2 is out of the running)
            remap.ran_out.append(1)
            remap._start_join(1, 3)              # T1 runs out now: queued, a join is running
            g.run_script_from_command("_INDX_REMAP_JOINED")
            return
        orig(gc)
    g.register_command("_INDX_SPOOL_JOIN", None)
    g.register_command("_INDX_SPOOL_JOIN", join_with_second_runout)
    afc.lanes["lane1"].prep_switch(False)
    p.reactor.run()
    check(tag + "beta.5 queued join (a second runout during a join): starts after the first, no waiting on the lock (stock waits forever)",
          no_deadlock() and sv["active_tool"] == 3 and "spool join T1 -> T3" in LOG, LOG)

    # beta.5: the runout arrives while its tool is being picked up
    p, afc, api, remap, b = setup(remap_cls=remap_cls)
    start(p, remap, partner={1: 3})
    sv = p.objects["save_variables"].allVariables
    sv["t2_material"] = sv["t0_material"] = "PETG"

    def busy(on):
        if fake:
            remap.change_busy = on
        p.objects["gcode_macro _TOOLCHANGE_VARS"].variables["change_busy"] = 1 if on else 0
    busy(True)                                   # the change to T1 is running, T1 already on the head
    sv["active_tool"] = 1
    p.reactor.tick()
    afc.lanes["lane2"].prep_switch(False)
    p.reactor.run()
    check(tag + "beta.5 runout during a tool change: held until the change ends",
          remap.join_busy in ((1, 3), [(1, 3)]) and not any(l.startswith("spool join") for l in LOG), (remap.join_busy, LOG))
    busy(False)
    p.reactor.tick()
    check(tag + "beta.5 runout during the pickup of the empty tool: the join starts after the change (stock only remaps: the tool prints on empty)",
          "spool join T1 -> T3" in LOG and sv["active_tool"] == 3 and no_deadlock(), LOG)
    # beta.7: INDX leaves runouts to a registered tool manager; the bridge
    # keeps INDX's spool join deciding (it feeds the tools, it does not own them)
    check(tag + "the bridge is INDX's tool manager and INDX's spool join still decides (beta.7: no 'Left to the tool manager')",
          getattr(api.manager, "name", "") == "indx_afc" and not any("Left to the tool manager" in str(l) for l in LOG), LOG)
    fixed = b._remap_fixed()
    wrapped = "INDX_RUNOUT" in b.prev and "_INDX_REMAP_JOINED" in b.prev
    check(tag + ("INDX's spool join is fixed (beta.7): the bridge's INDX_RUNOUT / _INDX_REMAP_JOINED workarounds stand aside"
                 if fixed else "INDX's spool join is beta.5/6: the bridge's workarounds are in place"),
          (not wrapped) if fixed else wrapped, (fixed, sorted(b.prev)))


run_all()
spool_join(Remap, "")

# ---- the same against INDX's real indx_remap.py ----------------------------
real = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("INDX_REMAP_PY", "")
if real:
    import importlib.util
    spec = importlib.util.spec_from_file_location("indx_remap_real", real)
    rm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rm)

    class RealRemap(rm.IndxRemap):
        def __init__(self, printer):
            printer.objects.setdefault("configfile", types.SimpleNamespace(get_status=lambda t: {"settings": {}}))
            rm.IndxRemap.__init__(self, Config(printer, {"runout_sensors": "", "join_retract": 0.8, "remap": True, "spool_join": True}))

    print("\n--- with INDX's own indx_remap.py: %s" % real)
    spool_join(RealRemap, "real: ")
else:
    print("\n(give the path of INDX's klipper_extras/indx_remap.py to run the spool join tests against it too)")

print("\n%s" % ("all ok" if not FAILS else "%d FAILED: %s" % (len(FAILS), "; ".join(FAILS))))
sys.exit(1 if FAILS else 0)
