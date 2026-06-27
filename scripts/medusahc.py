# klippy/extras/medusahc.py
#
# MedusaHC toolchanger orchestrator: sensors, state, validation, Mainsail UI sync.
# Motion sequences live in config/medusahc/macros.cfg; Python calls them via gcode.

import ast
import json
import logging


class GcodeMacroButton:
    """Expose a Python command as gcode_macro object for Mainsail/Fluidd UI."""

    def __init__(self, alias, handler):
        self.alias = str(alias)
        self.handler = handler
        self.variables = {}

    def get_status(self, eventtime):
        return self.variables

    def cmd(self, gcmd):
        self.handler(gcmd)


class ToolButtonMacro:
    """Mainsail/Fluidd tool buttons: gcode_macro T{n} with active/color variables."""

    cmd_SET_GCODE_VARIABLE_help = "Set the value of a G-Code macro variable"

    def __init__(self, medusa, tool_index, default_color):
        self.medusa = medusa
        self.tool_index = int(tool_index)
        self.alias = "T%d" % self.tool_index
        self.variables = {"active": 0, "color": str(default_color)}

    def get_status(self, eventtime):
        return self.variables

    def cmd(self, gcmd):
        self.medusa.select_tool(self.tool_index, gcmd)

    def cmd_SET_GCODE_VARIABLE(self, gcmd):
        variable = gcmd.get("VARIABLE")
        value = gcmd.get("VALUE")
        if variable not in self.variables:
            raise gcmd.error("Unknown gcode_macro variable '%s'" % (variable,))
        try:
            literal = ast.literal_eval(value)
            json.dumps(literal, separators=(",", ":"))
        except (SyntaxError, TypeError, ValueError) as e:
            raise gcmd.error(
                "Unable to parse '%s' as a literal: %s" % (value, e))
        updated = dict(self.variables)
        updated[variable] = literal
        self.variables = updated


class MedusaHC:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.buttons = self.printer.load_object(config, "buttons")

        self._init_delay = config.getfloat("init_delay", 2.0, minval=0.0)
        self.verbose = int(config.get("verbose", 0)) != 0
        self.assign_delay = float(config.get("assign_delay", 0.0))

        self.sync_mainsail_tools = int(config.get("sync_mainsail_tools", 0)) != 0
        self.sync_mainsail_sensors = int(config.get("sync_mainsail_sensors", 0)) != 0
        self.color_pressed = str(config.get("color_pressed", "00C853")).strip()
        self.color_released = str(config.get("color_released", "D32F2F")).strip()
        self.color_active = str(config.get("color_active", "1976D2")).strip()
        self.tool_button_color = str(config.get("tool_button_color", "00FF00")).strip()
        self.servo_name = str(config.get("servo", "my_servo")).strip()
        self._last_ui_active = None
        self._last_ui_lamp = {}
        self.tool_buttons = []

        self.common_cfg = {
            "y_safe": config.getfloat("y_safe", -5.0),
            "y_latch": config.getfloat("y_latch", -57.6),
            "x_shift": config.getfloat("x_shift", 9.0),
            "fast_accel": config.getfloat("fast_accel", 10000.0),
            "fast_speed": config.getfloat("fast_speed", 300.0),
            "y_prime": config.getfloat("y_prime", -52.0),
            "y_brush": config.getfloat("y_brush", -38.0),
            "x_prime_shift": config.getfloat("x_prime_shift", 12.0),
            "x_clean_move": config.getfloat("x_clean_move", 10.0),
            "e_open": config.getfloat("e_open", -4.0),
            "e_close": config.getfloat("e_close", 0.5),
            "e_cur_high_mult": config.getfloat("e_cur_high_mult", 1.6),
        }
        self.e_run_current_override = config.getfloat("e_run_current", 0.0)

        self.eddy_tap_x = config.getfloat("eddy_tap_x", 150.0)
        self.eddy_tap_y = config.getfloat("eddy_tap_y", 150.0)
        self.eddy_tap_z = config.getfloat("eddy_tap_z", 10.0)
        self.eddy_tap_f = config.getfloat("eddy_tap_f", 10000.0)
        self.eddy_park_x = config.getfloat("eddy_park_x", 20.0)
        self.eddy_park_y = config.getfloat("eddy_park_y", 220.0)

        self.tool_count = self._derive_tool_count(config)
        self.tool_profiles = {}
        self.offsets = {}
        self._load_tool_profiles(config)

        self._machine_state = "uninitialized"
        self.runtime_global = {
            "layer": 0,
            "e_cur": 0.0,
            "e_cur_high": 0.0,
            "target_tool": -1,
            "eddy_z": 0.0,
            "t0_probe_z": 0.0,
            "error_state": 0,
            "feeder_open": 0,
        }

        self.state = {}
        self.pin_by_label = {}
        self.t_indices = set()
        self.current_tool = -2
        self._compute_timer = None
        self._pending_reason = "startup"
        self._watch_enabled = False

        self._setup_switch_watch(config)
        self._register_commands()
        self._register_ui_macros()
        self._register_tool_buttons()
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _derive_tool_count(self, config):
        tool_indices = []
        for sec in config.get_prefix_sections("medusahc_tool "):
            try:
                t = int(sec.get_name().split(" ", 1)[1].strip())
            except Exception:
                continue
            tool_indices.append(t)
        if not tool_indices:
            raise config.error("medusahc: no [medusahc_tool N] sections found")
        tool_indices.sort()
        if tool_indices != list(range(len(tool_indices))):
            raise config.error("medusahc_tool sections must be contiguous from 0")
        return len(tool_indices)

    def _default_tool_profile(self, t):
        return {
            "x_base": 10.0 + (65.0 * int(t)),
            "offset_x": 0.0,
            "offset_y": 0.0,
            "offset_z": 0.0,
            "prime_amount": 8.0,
            "prime_speed": 20.0 if int(t) == 1 else 30.0,
            "prime_retract": 0.5,
            "prime_retract_speed": 30.0,
            "clean_move": 1,
            "clean_move_x": 10.0,
            "clean_move_y": -2.0,
            "clean_move_speed": 20.0,
            "clean_retract": 1.5,
            "clean_retract_speed": 30.0,
            "first_prime_flag": 1,
            "first_prime_amount": 15.0,
            "first_prime_speed": 15.0,
            "dock_pin": None,
        }

    def _load_tool_profiles(self, config):
        for t in range(self.tool_count):
            p = self._default_tool_profile(t)
            self.tool_profiles[t] = p
            self.offsets[t] = {
                "x": float(p["offset_x"]),
                "y": float(p["offset_y"]),
                "z": float(p["offset_z"]),
            }

        for sec in config.get_prefix_sections("medusahc_tool "):
            try:
                t = int(sec.get_name().split(" ", 1)[1].strip())
            except Exception:
                continue
            if t < 0 or t >= self.tool_count:
                continue
            p = self.tool_profiles[t]
            p["dock_pin"] = sec.get("dock_pin", p.get("dock_pin"))
            for key, conv, default in [
                ("x_base", float, p["x_base"]),
                ("offset_x", float, p["offset_x"]),
                ("offset_y", float, p["offset_y"]),
                ("offset_z", float, p["offset_z"]),
                ("prime_amount", float, p["prime_amount"]),
                ("prime_speed", float, p["prime_speed"]),
                ("prime_retract", float, p["prime_retract"]),
                ("prime_retract_speed", float, p["prime_retract_speed"]),
                ("clean_move_x", float, p["clean_move_x"]),
                ("clean_move_y", float, p["clean_move_y"]),
                ("clean_move_speed", float, p["clean_move_speed"]),
                ("clean_retract", float, p["clean_retract"]),
                ("clean_retract_speed", float, p["clean_retract_speed"]),
                ("first_prime_amount", float, p["first_prime_amount"]),
                ("first_prime_speed", float, p["first_prime_speed"]),
            ]:
                p[key] = conv(sec.getfloat(key, default))
            p["clean_move"] = int(sec.getint("clean_move", int(p["clean_move"])))
            p["first_prime_flag"] = int(
                sec.getint("first_prime_flag", int(p["first_prime_flag"]))
            )
            self.offsets[t]["x"] = float(p["offset_x"])
            self.offsets[t]["y"] = float(p["offset_y"])
            self.offsets[t]["z"] = float(p["offset_z"])

    def _handle_ready(self):
        when = self.reactor.monotonic() + self._init_delay
        self.reactor.register_timer(self._init_timer_cb, when)

    def _init_timer_cb(self, eventtime):
        try:
            self.cmd_INIT_SENSOR_STATE(None)
        except Exception:
            logging.exception("medusahc: INIT_SENSOR_STATE failed")
            self._respond("medusahc: INIT_SENSOR_STATE failed (see klippy.log)")
        return self.reactor.NEVER

    def _setup_switch_watch(self, config):
        pin_e = config.get("pin_e", None)
        pin_items = []
        if pin_e is not None:
            pin_items.append(("e", str(pin_e).strip()))
        for t in sorted(self.tool_profiles.keys()):
            dock_pin = self.tool_profiles[t].get("dock_pin")
            if dock_pin:
                pin_items.append(("t%d" % t, str(dock_pin).strip()))

        if not pin_items:
            self._watch_enabled = False
            return

        self._watch_enabled = True
        for label, pin_str in pin_items:
            self.pin_by_label[label] = pin_str
            if label not in self.state:
                self.state[label] = 0
            ti = self._parse_t_index(label)
            if ti is not None:
                self.t_indices.add(ti)
            self.buttons.register_debounce_button(
                pin_str, self._make_callback(label), config
            )

        if self.verbose:
            self._info(
                "medusahc: configured %d switch pin(s): %s"
                % (
                    len(pin_items),
                    ", ".join(
                        "%s=%s" % (l, self.pin_by_label[l])
                        for l in sorted(self.pin_by_label)
                    ),
                )
            )
        self._schedule_compute("startup", 0.0)

    def _register_commands(self):
        cmds = [
            ("INIT_SENSOR_STATE", self.cmd_INIT_SENSOR_STATE),
            ("DROP", self.cmd_DROP),
            ("DROP_CLOSE", self.cmd_DROP_CLOSE),
            ("DROP_TOOL", self.cmd_DROP_TOOL),
            ("ERROR", self.cmd_ERROR),
            ("TOOL_OFFSET_T", self.cmd_TOOL_OFFSET_T),
            ("LAYER_SET", self.cmd_LAYER_SET),
            ("PRIME_FLAGS_CLEAR", self.cmd_PRIME_FLAGS_CLEAR),
            ("PRIME_FLAGS_SET", self.cmd_PRIME_FLAGS_SET),
            ("SET_TOOL_Z_OFFSET", self.cmd_SET_TOOL_Z_OFFSET),
            ("_SET_FIRST_PRIME_FLAG", self.cmd_SET_FIRST_PRIME_FLAG),
            ("CLEAR_ERROR", self.cmd_CLEAR_ERROR),
        ]
        for name, fn in cmds:
            self.gcode.register_command(name, fn)

    def _register_ui_macros(self):
        """Register OPEN/CLOSE/CLEAN and calibration macros for Mainsail/Fluidd."""
        ui_macros = [
            ("OPEN", self.cmd_OPEN, "Open feeder latch"),
            ("CLOSE", self.cmd_CLOSE, "Close feeder latch"),
            ("CLEAN", self.cmd_CLEAN, "Clean active tool nozzle on brush"),
            ("CALIBRATE_AND_SAVE_TOOL_Z_EDDY",
             self.cmd_CALIBRATE_AND_SAVE_TOOL_Z_EDDY,
             "Eddy-ng tap Z for all tools, save, park"),
        ]
        for name, handler, desc in ui_macros:
            btn = GcodeMacroButton(name, handler)
            self.printer.add_object("gcode_macro %s" % name, btn)
            self.gcode.register_command(name, btn.cmd, desc=desc)

    def _register_tool_buttons(self):
        self.tool_buttons = []
        for i in range(self.tool_count):
            btn = ToolButtonMacro(self, i, self.tool_button_color)
            name = btn.alias
            self.printer.add_object("gcode_macro T%d" % i, btn)
            self.gcode.register_command(
                name, btn.cmd, desc="Select tool T%d" % i)
            self.gcode.register_mux_command(
                "SET_GCODE_VARIABLE", "MACRO", name,
                btn.cmd_SET_GCODE_VARIABLE,
                desc=btn.cmd_SET_GCODE_VARIABLE_help)
            self.tool_buttons.append(btn)

    def _run(self, line):
        self.gcode.run_script_from_command(line)

    def _respond(self, msg):
        self.gcode.respond_info(str(msg))

    def _now(self):
        return self.reactor.monotonic()

    def _status(self, obj_name):
        obj = self.printer.lookup_object(obj_name, None)
        if obj is None:
            return {}
        try:
            return obj.get_status(self._now())
        except Exception:
            return {}

    def _set_state(self, state):
        self._machine_state = str(state)

    def _state_string(self):
        if int(self.runtime_global.get("error_state", 0)):
            return "error"
        if self.current_tool == -2 and self._machine_state == "ready":
            return "error"
        return self._machine_state

    def get_status(self, eventtime):
        s = {
            "state": self._state_string(),
            "current_tool": int(self.current_tool),
            "target_tool": int(self.runtime_global["target_tool"]),
            "tool_count": int(self.tool_count),
            "feeder_open": bool(int(self.runtime_global["feeder_open"])),
            "error": bool(int(self.runtime_global["error_state"])),
            "layer": int(self.runtime_global["layer"]),
            "head_loaded": bool(int(self.state.get("e", 0))),
            "eddy_z": float(self.runtime_global["eddy_z"]),
            "t0_probe_z": float(self.runtime_global["t0_probe_z"]),
            "e_cur": float(self.runtime_global["e_cur"]),
            "e_cur_high": float(self.runtime_global["e_cur_high"]),
            "servo": self.servo_name,
        }
        for key, val in self.common_cfg.items():
            s[key] = float(val)
        for t in range(self.tool_count):
            p = self.tool_profiles[t]
            off = self.offsets[t]
            s["tool%d_docked" % t] = bool(int(self.state.get("t%d" % t, 0)))
            s["tool%d_x_base" % t] = float(p["x_base"])
            s["tool%d_offset_x" % t] = float(off["x"])
            s["tool%d_offset_y" % t] = float(off["y"])
            s["tool%d_offset_z" % t] = float(off["z"])
            s["tool%d_prime_amount" % t] = float(p["prime_amount"])
            s["tool%d_prime_speed" % t] = float(p["prime_speed"])
            s["tool%d_prime_retract" % t] = float(p["prime_retract"])
            s["tool%d_prime_retract_speed" % t] = float(p["prime_retract_speed"])
            s["tool%d_clean_move" % t] = int(p["clean_move"])
            s["tool%d_clean_move_x" % t] = float(p["clean_move_x"])
            s["tool%d_clean_move_y" % t] = float(p["clean_move_y"])
            s["tool%d_clean_move_speed" % t] = float(p["clean_move_speed"])
            s["tool%d_clean_retract" % t] = float(p["clean_retract"])
            s["tool%d_clean_retract_speed" % t] = float(p["clean_retract_speed"])
            s["tool%d_first_prime_flag" % t] = int(p["first_prime_flag"])
            s["tool%d_first_prime_amount" % t] = float(p["first_prime_amount"])
            s["tool%d_first_prime_speed" % t] = float(p["first_prime_speed"])
        return s

    def _parse_t_index(self, label):
        if not label.startswith("t"):
            return None
        try:
            return int(label[1:])
        except Exception:
            return None

    def _make_callback(self, label):
        def _cb(eventtime, state):
            try:
                s = int(state)
                if self.state.get(label, None) == s:
                    return
                self.state[label] = s
                if self.verbose:
                    self._info("medusahc: %s -> %d" % (label, s))
                if self.sync_mainsail_sensors:
                    ti = self._parse_t_index(label)
                    if ti is not None:
                        self._update_lamp(ti, s)
                self._schedule_compute(label, self.assign_delay)
            except Exception:
                logging.exception("medusahc: pin callback (%s)", label)

        return _cb

    def _compute_current_tool(self):
        n_tools = self.tool_count
        if n_tools < 1:
            return -2, (n_tools, None, None, None, 1)

        ex = int(self.state.get("e", 0))
        bad = 0
        if ex not in (0, 1):
            bad = 1

        occupied_sum = 0
        empties = 0
        empty_idx = -1

        for i in range(n_tools):
            occ = int(self.state.get("t%d" % i, 0))
            if occ not in (0, 1):
                bad = 1
            occupied_sum += occ
            if occ == 0:
                empties += 1
                empty_idx = i

        if bad == 1:
            ct = -2
        elif ex == 0 and occupied_sum == n_tools:
            ct = -1
        elif ex == 1 and occupied_sum == (n_tools - 1) and empties == 1:
            ct = empty_idx
        else:
            ct = -2

        return ct, (n_tools, ex, occupied_sum, empties, bad)

    def _schedule_compute(self, reason, delay):
        self._pending_reason = reason
        if self._compute_timer is not None:
            try:
                self.reactor.unregister_timer(self._compute_timer)
            except Exception:
                pass
            self._compute_timer = None
        when = self.reactor.monotonic() + max(0.0, float(delay))
        self._compute_timer = self.reactor.register_timer(
            self._compute_timer_cb, when
        )

    def _compute_timer_cb(self, eventtime):
        self._compute_timer = None
        try:
            ct, dbg = self._compute_current_tool()
            self.current_tool = int(ct)
            if self.verbose:
                self._info(
                    "medusahc: current_tool=%d (reason=%s dbg=%s)"
                    % (self.current_tool, self._pending_reason, str(dbg))
                )
            if self.sync_mainsail_tools:
                self._sync_mainsail_tools(self.current_tool)
            if self.sync_mainsail_sensors:
                self._refresh_lamps()
        except Exception:
            logging.exception("medusahc: compute/apply failed")
        return self.reactor.NEVER

    def _info(self, msg):
        if not self.verbose:
            return
        try:
            self.gcode.respond_info(msg)
        except Exception:
            logging.info(msg)

    def _require_t(self, gcmd, name):
        try:
            return int(gcmd.get_int(name))
        except Exception:
            raise gcmd.error("%s: missing %s" % (gcmd.get_command(), name))

    def _validate_t(self, gcmd, t, where):
        if t < 0 or t >= self.tool_count:
            raise gcmd.error("%s: bad T" % where)

    def _print_state(self):
        ps = self.printer.lookup_object("print_stats", None)
        return str(getattr(ps, "state", "")) if ps is not None else ""

    def _home_request(self):
        homed = str(self._status("toolhead").get("homed_axes", ""))
        if "x" not in homed or "y" not in homed:
            self._respond("homing (G28)")
            self._run("G28")

    def _set_offset(self, t, x, y, z):
        self.offsets[t] = {"x": float(x), "y": float(y), "z": float(z)}

    def save_tool_offsets(self, t):
        off = self.offsets.get(t, {"x": 0.0, "y": 0.0, "z": 0.0})
        x = float(off.get("x", 0.0))
        y = float(off.get("y", 0.0))
        z = float(off.get("z", 0.0))
        cfg = self.printer.lookup_object("configfile", None)
        if cfg is None:
            raise self.gcode.error("save_tool_offsets: configfile not available")
        sec = "medusahc_tool %d" % t
        cfg.set(sec, "offset_x", "%.6f" % x)
        cfg.set(sec, "offset_y", "%.6f" % y)
        cfg.set(sec, "offset_z", "%.6f" % z)
        self._respond(
            "SAVE_TOOL_OFFSETS: T%d X=%.6f Y=%.6f Z=%.6f" % (t, x, y, z))

    def notify_save_config(self):
        self._respond(
            "Offsets staged in [medusahc_tool N]. Run SAVE_CONFIG to write\n"
            "them to printer.cfg and restart Klipper."
        )

    def _require_eddy(self, gcmd):
        cmds = getattr(self.gcode, "commands", None)
        if cmds is not None and cmds.get("PROBE_EDDY_NG_TOOL_TAP") is not None:
            return
        for name in self.printer.objects:
            if name.startswith("probe_eddy"):
                return
        raise gcmd.error(
            "eddy-ng not loaded; install probe_eddy_ng and configure tap")

    def _move_to_eddy_tap(self, gcmd):
        x = gcmd.get_float("X", self.eddy_tap_x)
        y = gcmd.get_float("Y", self.eddy_tap_y)
        z = gcmd.get_float("Z", self.eddy_tap_z)
        f = gcmd.get_float("F", self.eddy_tap_f)
        self._run("BED_MESH_CLEAR")
        self._run("G0 Z%.6f F%.6f" % (z, f))
        self._run("G0 X%.6f Y%.6f F%.6f" % (x, y, f))

    def _tap_eddy_tool_z(self, gcmd):
        self._require_eddy(gcmd)
        self._move_to_eddy_tap(gcmd)
        self._run("PROBE_EDDY_NG_TOOL_TAP")

    def cmd_CALIBRATE_AND_SAVE_TOOL_Z_EDDY(self, gcmd):
        drop = int(gcmd.get_int("DROP", 1))
        saved = False
        for t in range(self.tool_count):
            self.select_tool(t)
            self._tap_eddy_tool_z(gcmd)
            if t > 0:
                self.save_tool_offsets(t)
                saved = True
        if drop:
            self._run("DROP_CLOSE")
            self._run(
                "G1 X%.6f Y%.6f F10000"
                % (self.eddy_park_x, self.eddy_park_y))
        if saved:
            self.notify_save_config()

    def _load_extruder_run_current(self):
        e_cur = 0.0
        source = "unknown"

        if self.e_run_current_override > 0.0:
            e_cur = float(self.e_run_current_override)
            source = "e_run_current"
        else:
            for name in (
                "tmc2209 extruder",
                "tmc2130 extruder",
                "tmc5160 extruder",
                "tmc2240 extruder",
                "tmc2660 extruder",
            ):
                try:
                    tmc = self.printer.lookup_object(name)
                    e_cur = float(getattr(tmc, "run_current", 0.0))
                    if e_cur > 0.0:
                        source = name
                        break
                except Exception:
                    pass

            if e_cur <= 0.0:
                cfg = self.printer.lookup_object("configfile", None)
                settings = getattr(cfg, "settings", {}) if cfg is not None else {}
                for name in (
                    "tmc2209 extruder",
                    "tmc2130 extruder",
                    "tmc5160 extruder",
                    "tmc2240 extruder",
                    "tmc2660 extruder",
                ):
                    tmc_e = settings.get(name, {})
                    if isinstance(tmc_e, dict):
                        e_cur = float(tmc_e.get("run_current", 0.0))
                        if e_cur > 0.0:
                            source = name + " (config)"
                            break

            if e_cur <= 0.0:
                e_cur = 0.5
                source = "default"
                self._respond(
                    "medusahc: WARN extruder run_current not found; "
                    "using %.2fA (set e_run_current in [medusahc] or add [tmc* extruder])"
                    % e_cur
                )

        e_mult = float(self.common_cfg["e_cur_high_mult"])
        self.runtime_global["e_cur"] = e_cur
        self.runtime_global["e_cur_high"] = e_cur * e_mult
        if self.verbose:
            self._respond(
                "medusahc: e_cur=%.3f e_cur_high=%.3f (%s)"
                % (e_cur, e_cur * e_mult, source)
            )

    def cmd_OPEN(self, gcmd):
        if int(self.runtime_global.get("feeder_open", 0)) == 1:
            return
        if float(self.runtime_global.get("e_cur", 0.0)) <= 0.0:
            self._load_extruder_run_current()
        self._run("_OPEN_START")
        self._run("_OPEN_MOVE")
        self.runtime_global["feeder_open"] = 1

    def cmd_CLOSE(self, gcmd):
        if float(self.runtime_global.get("e_cur", 0.0)) <= 0.0:
            self._load_extruder_run_current()
        self._run("_CLOSE_MOVE")
        self.runtime_global["feeder_open"] = 0

    def _ensure_open(self):
        self.cmd_OPEN(None)

    def _ensure_closed(self):
        self.cmd_CLOSE(None)

    def _pre_pickup_checks(self, t):
        if int(self.runtime_global.get("error_state", 0)) == 1:
            self._respond("pre_pickup: paused after error")
            return False
        ct = self.current_tool
        if ct == -2:
            self._respond("pre_pickup: sensor error")
            self.cmd_ERROR(None)
            return False
        if ct == t:
            self._run("TOOL_OFFSET_T T=%d" % t)
            self._respond("pre_pickup: tool already installed")
            return False
        if ct >= 0 and ct < self.tool_count and ct != t:
            self._respond("pre_pickup: wrong tool on head")
            self.cmd_ERROR(None)
            return False
        if ct != -1:
            self._respond("pre_pickup: invalid current_tool")
            self.cmd_ERROR(None)
            return False
        return True

    def _pre_drop_checks(self):
        ct = self.current_tool
        if ct == -2:
            self._respond("pre_drop: sensor error")
            self.cmd_ERROR(None)
            return False
        if ct == -1:
            self._respond("pre_drop: nothing installed")
            return False
        if ct < 0 or ct >= self.tool_count:
            self._respond("pre_drop: invalid current_tool")
            self.cmd_ERROR(None)
            return False
        return True

    def _verify_pickup(self, t):
        ct = self.current_tool
        if ct != t:
            self._respond("verify_pickup: mismatch (ct=%d need=%d)" % (ct, t))
            self.cmd_ERROR(None)
            return False
        self._respond("verify_pickup OK")
        return True

    def _verify_drop(self):
        ct = self.current_tool
        if ct != -1:
            self._respond("verify_drop: mismatch (ct=%d)" % ct)
            self.cmd_ERROR(None)
            return False
        self._respond("verify_drop OK")
        return True

    def _do_pickup(self, t):
        self._ensure_open()
        self._run("_SET_MOVE T=%d" % t)
        self._run("M106 S255")
        try:
            self._run("G4 P1000")
            if not self._verify_pickup(t):
                return False
            self._run("_POST_PICKUP T=%d" % t)
            self.runtime_global["feeder_open"] = 0
            return True
        finally:
            self._run("M106 S0")

    def _do_drop(self):
        ct = self.current_tool
        self._ensure_open()
        self._run("_DROP_MOVE T=%d" % ct)
        self._run("G4 P900")
        return self._verify_drop()

    def _sync_mainsail_tools(self, ct):
        active = int(ct) if ct >= 0 else -1
        if self._last_ui_active == active:
            return
        for i, btn in enumerate(self.tool_buttons):
            updated = dict(btn.variables)
            updated["active"] = 1 if i == active else 0
            btn.variables = updated
        self._last_ui_active = active

    def _update_lamp(self, ti, state):
        if ti >= self.tool_count:
            return
        if ti == int(self.current_tool):
            color = self.color_active
        else:
            color = self.color_pressed if int(state) == 1 else self.color_released
        if self._last_ui_lamp.get(ti) == color:
            return
        try:
            btn = self.tool_buttons[ti]
            updated = dict(btn.variables)
            updated["color"] = color
            btn.variables = updated
            self._last_ui_lamp[ti] = color
        except Exception:
            logging.exception("medusahc: failed T%d.color", ti)

    def _refresh_lamps(self):
        for i in range(self.tool_count):
            s = int(self.state.get("t%d" % i, 0))
            self._update_lamp(i, s)

    def cmd_INIT_SENSOR_STATE(self, gcmd):
        self._respond("INIT_SENSOR_STATE")

        self.cmd_CLOSE(gcmd)
        self._load_extruder_run_current()
        self.runtime_global["feeder_open"] = 0

        for t in range(self.tool_count):
            off = self.offsets.get(t, {"x": 0.0, "y": 0.0, "z": 0.0})
            self._respond(
                "INIT TOOL_OFFSET T%d: X=%.6f Y=%.6f Z=%.6f"
                % (t, float(off["x"]), float(off["y"]), float(off["z"]))
            )

        self._set_state("ready")

    def select_tool(self, t, gcmd=None):
        t = int(t)
        if gcmd is None:
            gcmd = self.gcode.create_gcode_command(
                "T%d" % t, "T%d" % t, {"T": str(t)})
        self._validate_t(gcmd, t, "T%d" % t)

        self._set_state("changing")
        self.runtime_global["error_state"] = 0
        self.runtime_global["target_tool"] = t

        self._home_request()
        self._run("TOOL_OFFSET_T T=%d MOVE=0" % t)

        if self._print_state() == "printing":
            self._run("G91")
            self._run("G1 Y-2 F15000")
            self._run("G1 Z3 F14000")
            self._run("G90")
        else:
            self._run("G91")
            self._run("G1 Z1 F14000")
            self._run("G90")

        ct = self.current_tool
        if ct == -2:
            self.cmd_ERROR(gcmd)
            return
        if ct == t:
            self._respond("T%d: already selected" % t)
            self._set_state("ready")
            return

        if ct >= 0 and ct < self.tool_count and ct != t:
            if not self._pre_drop_checks():
                self._set_state("ready")
                return
            if not self._do_drop():
                return
            if int(self.runtime_global.get("error_state", 0)) == 1:
                return

        if not self._pre_pickup_checks(t):
            if int(self.runtime_global.get("error_state", 0)) == 0:
                self._set_state("ready")
            return

        if self._do_pickup(t):
            self._set_state("ready")

    def cmd_DROP(self, gcmd):
        self._set_state("changing")
        self._home_request()
        if not self._pre_drop_checks():
            self._set_state("ready")
            return
        if self._do_drop():
            self._set_state("ready")

    def cmd_DROP_CLOSE(self, gcmd):
        self._run("SAVE_GCODE_STATE")
        self._respond("DROP and CLOSE")
        self.cmd_DROP(gcmd)
        self._ensure_closed()

    def cmd_DROP_TOOL(self, gcmd):
        self._run("SAVE_GCODE_STATE")
        self._respond("DROP TOOL")
        self.cmd_DROP(gcmd)
        self._ensure_closed()

    def cmd_CLEAN(self, gcmd):
        ct = self.current_tool
        if ct < 0 or ct >= self.tool_count:
            self._respond("CLEAN: no valid tool selected")
            self.cmd_ERROR(gcmd)
            return
        self._home_request()
        self._run("_CLEAN_MOVE T=%d" % ct)

    def cmd_ERROR(self, gcmd):
        y_safe = float(self.common_cfg["y_safe"])
        target_tool = int(self.runtime_global.get("target_tool", -1))
        state = self._print_state()
        self.runtime_global["error_state"] = 1
        self._set_state("error")

        if state in ("printing", "paused"):
            self._respond("ERROR")
            self._run("G90")
            self._run("G1 Y%.6f F6000" % (y_safe + 50.0))
            self._run("PAUSE")
            self._respond("Error - Target tool - %d" % target_tool)
        else:
            self._respond("ERROR set (no print, no pause)")

    def cmd_TOOL_OFFSET_T(self, gcmd):
        t = self._require_t(gcmd, "T")
        move = int(gcmd.get_int("MOVE", 1))
        self._validate_t(gcmd, t, "TOOL_OFFSET_T")

        off = self.offsets.get(t, {"x": 0.0, "y": 0.0, "z": 0.0})
        x = float(off.get("x", 0.0))
        y = float(off.get("y", 0.0))
        z_off = float(off.get("z", 0.0))
        z_eddy = float(self.runtime_global.get("eddy_z", 0.0))
        z = z_off + z_eddy

        self._run(
            "SET_GCODE_OFFSET X=%.6f Y=%.6f Z=%.6f MOVE=%d" % (x, y, z, move)
        )
        self._respond(
            "TOOL_OFFSET_T: X=%.6f Y=%.6f Z=%.6f MOVE=%d" % (x, y, z, move)
        )

    def cmd_LAYER_SET(self, gcmd):
        try:
            self.runtime_global["layer"] = int(gcmd.get_int("L"))
        except Exception:
            return

    def cmd_PRIME_FLAGS_CLEAR(self, gcmd):
        for t in range(self.tool_count):
            self.tool_profiles[t]["first_prime_flag"] = 0
        self._respond("PRIME FLAG CLEAR")

    def cmd_PRIME_FLAGS_SET(self, gcmd):
        for t in range(self.tool_count):
            self.tool_profiles[t]["first_prime_flag"] = 1
        self._respond("PRIME FLAG SET")

    def cmd_SET_TOOL_Z_OFFSET(self, gcmd):
        v = gcmd.get_float("VALUE")
        ct = self.current_tool
        if ct < 0:
            raise gcmd.error("SET_TOOL_Z_OFFSET: no tool on head")
        if ct == 0:
            self.runtime_global["t0_probe_z"] = float(v)
            self._respond("SET_TOOL_Z_OFFSET: stored t0_probe_z=%.6f" % v)
            return
        base = float(self.runtime_global.get("t0_probe_z", 0.0))
        delta = float(v) - base
        off = self.offsets.get(ct, {"x": 0.0, "y": 0.0, "z": 0.0})
        off["z"] = delta
        self.offsets[ct] = off
        self._respond(
            "SET_TOOL_Z_OFFSET: T%d z_offset=%.6f (v=%.6f base=%.6f)"
            % (ct, delta, v, base)
        )

    def cmd_CLEAR_ERROR(self, gcmd):
        self.runtime_global["error_state"] = 0
        if self._machine_state == "error":
            self._set_state("ready")

    def cmd_SET_FIRST_PRIME_FLAG(self, gcmd):
        t = self._require_t(gcmd, "T")
        self._validate_t(gcmd, t, "_SET_FIRST_PRIME_FLAG")
        v = int(gcmd.get_int("VALUE", 1))
        self.tool_profiles[t]["first_prime_flag"] = v

    def set_offset(self, t, x, y, z):
        self._set_offset(t, x, y, z)


def load_config(config):
    return MedusaHC(config)
