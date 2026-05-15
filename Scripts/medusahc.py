# MedusaHC toolchanger logic ported from MHC_macros.cfg
#
# Usage in Klipper config (example):
#   [medusahc]
#
# This module registers command names that match the original macros,
# so existing slicer/start-end gcode can stay mostly unchanged.

import logging

class MedusaHC:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.buttons = self.printer.load_object(config, "buttons")
        self._init_delay = config.getfloat("init_delay", 2.0, minval=0.0)
        self._expose_internal_commands = int(config.get("expose_internal_commands", 0)) != 0

        # Native medusahc config values (macro-free mode)
        self.max_tool_cfg = self._resolve_max_tool(config)
        self.common_cfg = {
            "y_safe": config.getfloat("y_safe", -5.0),
            "y_latch": config.getfloat("y_latch", -57.6),
            "x_shift": config.getfloat("x_shift", 9.0),
            "fast_accel": config.getfloat("fast_accel", 10000.0),
            "fast_speed": config.getfloat("fast_speed", 300.0),
            "y_prime": config.getfloat("y_prime", -52.0),
            "y_brush": config.getfloat("y_brush", -38.0),
            "x_prime_shift": config.getfloat("x_prime_shift", 12.0),
            "e_open": config.getfloat("e_open", -4.0),
            "e_close": config.getfloat("e_close", 0.5),
            "e_cur_high_mult": config.getfloat("e_cur_high_mult", 1.6),
        }

        # Base X positions can be defined directly in [medusahc] via x_t0..x_tN
        self.x_base = {}
        for t in range(self.max_tool_cfg):
            self.x_base[t] = config.getfloat("x_t%d" % t, 10.0 + (65.0 * t))

        self.tool_profiles = {}
        self.offsets = {}
        self._load_tool_profiles(config)

        self.runtime_global = {
            "max_tool": int(self.max_tool_cfg),
            "layer": 0,
            "x_cur": 0.0,
            "x_cur_high": 0.0,
            "y_cur": 0.0,
            "y_cur_high": 0.0,
            "e_cur": 0.0,
            "e_cur_high": 0.0,
            "target_tool": -1,
            "eddy_z": 0.0,
            "error_state": 0,
            "feeder_open": 0,
        }

        # Integrated switch watcher settings
        self.verbose = int(config.get("verbose", 0)) != 0
        self.assign_delay = float(config.get("assign_delay", 0.0))

        self.state = {}
        self.pin_by_label = {}
        self.t_indices = set()
        self.current_tool = -2

        self._compute_timer = None
        self._pending_reason = "startup"
        self._internal_watch_enabled = False

        self._setup_switch_watch(config)

        self._register_commands()
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _resolve_max_tool(self, config):
        # Explicit setting wins.
        explicit = config.get("max_tool", None)
        if explicit is not None:
            return int(config.getint("max_tool", 4, minval=1))

        # Infer from configured tool sections/options.
        max_idx = -1

        for sec in config.get_prefix_sections("medusahc_tool "):
            try:
                t = int(sec.get_name().split(" ", 1)[1].strip())
            except Exception:
                continue
            if t > max_idx:
                max_idx = t

        for opt in config.get_prefix_options("pin_t"):
            # Example: pin_t3 -> index 3
            try:
                t = int(opt[len("pin_t"):])
            except Exception:
                continue
            if t > max_idx:
                max_idx = t

        for opt in config.get_prefix_options("x_t"):
            # Example: x_t3 -> index 3
            try:
                t = int(opt[len("x_t"):])
            except Exception:
                continue
            if t > max_idx:
                max_idx = t

        if max_idx >= 0:
            return max_idx + 1

        return 4

    def _default_tool_profile(self, t):
        prime_speed = 20.0 if int(t) == 1 else 30.0
        return {
            "x_base": self.x_base.get(int(t), 10.0 + (65.0 * int(t))),
            "offset_x": 0.0,
            "offset_y": 0.0,
            "offset_z": 0.0,
            "prime_amount": 8.0,
            "prime_speed": prime_speed,
            "prime_retract": 0.5,
            "prime_retract_speed": 30.0,
            "clean_move_x": 10.0,
            "clean_move_y": -2.0,
            "clean_move_speed": 20.0,
            "clean_retract": 1.5,
            "clean_retract_speed": 30.0,
            "first_prime_flag": 1,
            "first_prime_amount": 15.0,
            "first_prime_speed": 15.0,
        }

    def _load_tool_profiles(self, config):
        for t in range(self.max_tool_cfg):
            p = self._default_tool_profile(t)
            p["dock_pin"] = None
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

            if t < 0 or t >= self.max_tool_cfg:
                continue

            p = self.tool_profiles[t]
            p["dock_pin"] = sec.get("dock_pin", p.get("dock_pin", None))
            p["x_base"] = sec.getfloat("x_base", p["x_base"])
            p["offset_x"] = sec.getfloat("offset_x", p["offset_x"])
            p["offset_y"] = sec.getfloat("offset_y", p["offset_y"])
            p["offset_z"] = sec.getfloat("offset_z", p["offset_z"])

            p["prime_amount"] = sec.getfloat("prime_amount", p["prime_amount"])
            p["prime_speed"] = sec.getfloat("prime_speed", p["prime_speed"])
            p["prime_retract"] = sec.getfloat("prime_retract", p["prime_retract"])
            p["prime_retract_speed"] = sec.getfloat("prime_retract_speed", p["prime_retract_speed"])

            p["clean_move_x"] = sec.getfloat("clean_move_x", p["clean_move_x"])
            p["clean_move_y"] = sec.getfloat("clean_move_y", p["clean_move_y"])
            p["clean_move_speed"] = sec.getfloat("clean_move_speed", p["clean_move_speed"])
            p["clean_retract"] = sec.getfloat("clean_retract", p["clean_retract"])
            p["clean_retract_speed"] = sec.getfloat("clean_retract_speed", p["clean_retract_speed"])

            p["first_prime_flag"] = int(sec.getint("first_prime_flag", int(p["first_prime_flag"])))
            p["first_prime_amount"] = sec.getfloat("first_prime_amount", p["first_prime_amount"])
            p["first_prime_speed"] = sec.getfloat("first_prime_speed", p["first_prime_speed"])

            self.x_base[t] = float(p["x_base"])
            self.offsets[t]["x"] = float(p["offset_x"])
            self.offsets[t]["y"] = float(p["offset_y"])
            self.offsets[t]["z"] = float(p["offset_z"])

    # ---------- lifecycle ----------
    def _handle_ready(self):
        when = self.reactor.monotonic() + self._init_delay
        self.reactor.register_timer(self._init_timer_cb, when)

    def _init_timer_cb(self, eventtime):
        try:
            self._cmd_INIT_SENSOR_STATE(None)
        except Exception:
            logging.exception("medusahc: INIT_SENSOR_STATE failed")
            self._respond("medusahc: INIT_SENSOR_STATE failed (see klippy.log)")
        return self.reactor.NEVER

    def _setup_switch_watch(self, config):
        # Head sensor remains in [medusahc], tool dock sensors move to [medusahc_tool N].
        pin_e = config.get("pin_e", None)
        per_tool = []
        for t in sorted(self.tool_profiles.keys()):
            dock_pin = self.tool_profiles[t].get("dock_pin", None)
            if dock_pin:
                per_tool.append(("t%d" % t, str(dock_pin).strip()))

        if pin_e is None and not per_tool:
            self._internal_watch_enabled = False
            return

        self._internal_watch_enabled = True
        pin_items = []
        if pin_e is not None:
            pin_items.append(("e", str(pin_e).strip()))
        pin_items.extend(per_tool)

        for label, pin_str in pin_items:
            self.pin_by_label[label] = pin_str

            if label not in self.state:
                self.state[label] = 0

            ti = self._parse_t_index(label)
            if ti is not None:
                self.t_indices.add(ti)

            self.buttons.register_debounce_button(pin_str, self._make_callback(label), config)

        if self.verbose:
            self._info(
                "medusahc: configured %d switch pin(s): %s"
                % (
                    len(pin_items),
                    ", ".join(["%s=%s" % (l, self.pin_by_label[l]) for l in sorted(self.pin_by_label)]),
                )
            )

        self._schedule_compute("startup", 0.0)

    # ---------- helpers ----------
    def _register(self, name, fn, desc=""):
        self.gcode.register_command(name, fn, desc=desc)

    def _register_commands(self):
        # Public command surface used by external config/gcode.
        self._register("INIT_SENSOR_STATE", self.cmd_INIT_SENSOR_STATE)
        self._register("SET", self.cmd_SET)
        self._register("DROP", self.cmd_DROP)
        self._register("DROP_CLOSE", self.cmd_DROP_CLOSE)
        self._register("CLEAN", self.cmd_CLEAN)
        self._register("ERROR", self.cmd_ERROR)
        self._register("TOOL_OFFSET_T", self.cmd_TOOL_OFFSET_T)
        self._register("ASSIGN_TCH_TOOL", self.cmd_ASSIGN_TCH_TOOL)
        self._register("LAYER_SET", self.cmd_LAYER_SET)
        self._register("PRIME_FLAGS_CLEAR", self.cmd_PRIME_FLAGS_CLEAR)
        self._register("PRIME_FLAGS_SET", self.cmd_PRIME_FLAGS_SET)

        # Optional legacy/debug command aliases for step-by-step execution.
        if self._expose_internal_commands:
            self._register("OPEN", self.cmd_OPEN)
            self._register("CLOSE", self.cmd_CLOSE)
            self._register("OPEN_START", self.cmd_OPEN_START)
            self._register("OPEN_MOVE", self.cmd_OPEN_MOVE)
            self._register("SET_CHANGE_CHECK", self.cmd_SET_CHANGE_CHECK)
            self._register("SET_CHECK", self.cmd_SET_CHECK)
            self._register("SET_MOVE", self.cmd_SET_MOVE)
            self._register("VERIFY_SET", self.cmd_VERIFY_SET)
            self._register("DROP_CHECK", self.cmd_DROP_CHECK)
            self._register("DROP_MOVE", self.cmd_DROP_MOVE)
            self._register("VERIFY_DROP", self.cmd_VERIFY_DROP)

    def _run(self, line):
        self.gcode.run_script_from_command(line)

    def _cmd_t(self, name, t):
        return self.gcode.create_gcode_command(name, name, {"T": str(int(t))})

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

    def _macro(self, macro_name):
        if macro_name == "GLOBAL_STATE":
            return dict(self.runtime_global)

        if macro_name == "TOOL_CFG":
            out = dict(self.common_cfg)
            for t, x in self.x_base.items():
                out["x_t%d" % t] = x
            return out

        if macro_name.startswith("TOOL_STATE_"):
            try:
                t = int(macro_name.split("_", 2)[2])
            except Exception:
                return {}
            return dict(self.tool_profiles.get(t, self._default_tool_profile(t)))

        if macro_name == "TOOL_OFFSET":
            out = {}
            for t in range(self.max_tool_cfg):
                off = self.offsets.get(t, {"x": 0.0, "y": 0.0, "z": 0.0})
                out["t%d_off_x" % t] = float(off.get("x", 0.0))
                out["t%d_off_y" % t] = float(off.get("y", 0.0))
                out["t%d_off_z" % t] = float(off.get("z", 0.0))
            return out

        return {}

    def _pin_watch(self):
        return int(self.current_tool)

    def get_status(self, eventtime):
        return {"current_tool": int(self._pin_watch())}

    def _parse_t_index(self, label):
        if not label.startswith("t"):
            return None
        try:
            return int(label[1:])
        except Exception:
            return None

    def _tool_count(self):
        if not self.t_indices:
            return 0
        return max(self.t_indices) + 1

    def _make_callback(self, label):
        def _cb(eventtime, state):
            try:
                s = int(state)
                if self.state.get(label, None) == s:
                    return
                self.state[label] = s
                if self.verbose:
                    self._info("medusahc: %s -> %d (t=%.6f)" % (label, s, eventtime))
                self._schedule_compute(label, self.assign_delay)
            except Exception:
                logging.exception("medusahc: exception in pin callback (%s)", label)
                self._info("medusahc: ERROR in callback (%s) - see klippy.log" % label)

        return _cb

    def _compute_current_tool(self):
        n_tools = self._tool_count()
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
        self._compute_timer = self.reactor.register_timer(self._compute_timer_cb, when)

    def _compute_timer_cb(self, eventtime):
        self._compute_timer = None
        try:
            ct, dbg = self._compute_current_tool()
            self.current_tool = int(ct)

            n_tools, ex, occupied_sum, empties, bad = dbg
            self._info(
                "medusahc: APPLY current_tool=%d (reason=%s N=%s ex=%s S=%s empties=%s bad=%s)"
                % (
                    self.current_tool,
                    str(self._pending_reason),
                    str(n_tools),
                    str(ex),
                    str(occupied_sum),
                    str(empties),
                    str(bad),
                )
            )
        except Exception:
            logging.exception("medusahc: exception in compute/apply")
            self._info("medusahc: ERROR in compute/apply - see klippy.log")
        return self.reactor.NEVER

    def _info(self, msg):
        if not self.verbose:
            return
        try:
            self.gcode.respond_info(msg)
        except Exception:
            logging.info(msg)

    def _max_tool(self):
        return int(self.runtime_global.get("max_tool", self.max_tool_cfg))

    def _print_state(self):
        ps = self.printer.lookup_object("print_stats", None)
        return str(getattr(ps, "state", "")) if ps is not None else ""

    def _tool_cfg(self, key, default=0.0):
        if key in self.common_cfg:
            return float(self.common_cfg[key])
        if key.startswith("x_t"):
            try:
                t = int(key[3:])
                if t in self.x_base:
                    return float(self.x_base[t])
            except Exception:
                pass
        return float(default)

    def _tool_state(self, t):
        return self._macro("TOOL_STATE_%d" % int(t))

    def _tool_offset_state(self):
        return self._macro("TOOL_OFFSET")

    def _set_var(self, macro, var, value):
        if macro == "GLOBAL_STATE":
            self.runtime_global[var] = value
            return

        if macro == "TOOL_OFFSET":
            parts = str(var).split("_")
            # expected: t{idx}_off_{axis}
            if len(parts) == 3 and parts[0].startswith("t") and parts[1] == "off" and parts[2] in ("x", "y", "z"):
                try:
                    t = int(parts[0][1:])
                except Exception:
                    return
                axis = parts[2]
                if t not in self.offsets:
                    self.offsets[t] = {"x": 0.0, "y": 0.0, "z": 0.0}
                self.offsets[t][axis] = float(value)
            return

        if macro.startswith("TOOL_STATE_"):
            try:
                t = int(macro.split("_", 2)[2])
            except Exception:
                t = None
            if t is not None:
                if t not in self.tool_profiles:
                    self.tool_profiles[t] = self._default_tool_profile(t)
                self.tool_profiles[t][var] = value
            return

    def _require_t(self, gcmd, name):
        try:
            return int(gcmd.get_int(name))
        except Exception:
            raise gcmd.error("%s: missing %s" % (gcmd.get_command(), name))

    def _validate_t(self, gcmd, t, where):
        max_tool = self._max_tool()
        if t < 0 or t >= max_tool:
            raise gcmd.error("%s: bad T" % where)

    def _home_request(self):
        homed = str(self._status("toolhead").get("homed_axes", ""))
        if "x" not in homed or "y" not in homed:
            self._respond("SET: homing (G28)")
            self._run("G28")

    # ---------- core commands ----------
    def _cmd_INIT_SENSOR_STATE(self, gcmd):
        self._respond("INIT_SENSOR_STATE")

        max_tool = self._max_tool()

        cfg = self.printer.lookup_object("configfile", None)
        settings = getattr(cfg, "settings", {}) if cfg is not None else {}

        tmc_e = settings.get("tmc2209 extruder", {})

        e_cur = float(tmc_e.get("run_current", 0.0))
        e_mult = self._tool_cfg("e_cur_high_mult", 1.6)

        e_hi = e_cur * e_mult

        sv_obj = self.printer.lookup_object("save_variables", None)
        sv = {}
        if sv_obj is not None:
            sv = self._status("save_variables").get("variables", {})
            if not isinstance(sv, dict):
                sv = {}

        self.cmd_CLOSE(gcmd)
        self._set_var("GLOBAL_STATE", "e_cur", e_cur)

        self._set_var("GLOBAL_STATE", "e_cur_high", e_hi)

        for t in range(max_tool):
            sx = "t%d_gcode_x_offset" % t
            sy = "t%d_gcode_y_offset" % t
            sz = "t%d_gcode_z_offset" % t
            if sx in sv and sy in sv and sz in sv:
                x = float(sv[sx])
                y = float(sv[sy])
                z = float(sv[sz])
                self._set_var("TOOL_OFFSET", "t%d_off_x" % t, x)
                self._set_var("TOOL_OFFSET", "t%d_off_y" % t, y)
                self._set_var("TOOL_OFFSET", "t%d_off_z" % t, z)
                self._respond("INIT TOOL_OFFSET T%d: X=%.6f Y=%.6f Z=%.6f" % (t, x, y, z))

    def cmd_INIT_SENSOR_STATE(self, gcmd):
        self._cmd_INIT_SENSOR_STATE(gcmd)

    def cmd_OPEN(self, gcmd):
        self.cmd_OPEN_START(gcmd)
        self.cmd_OPEN_MOVE(gcmd)

    def cmd_CLOSE(self, gcmd):
        e_close = self._tool_cfg("e_close", 0.5)
        self._run("SET_SERVO SERVO=my_servo ANGLE=0")
        self._set_var("GLOBAL_STATE", "feeder_open", 0)
        self._run("G91")
        self._run("G1 E%.6f F6000" % e_close)
        self._run("G90")
        self._run("G4 P400")

    def cmd_OPEN_START(self, gcmd):
        high_cur = float(self._macro("GLOBAL_STATE").get("e_cur_high", 0.0))
        self._run("SET_TMC_CURRENT STEPPER=extruder CURRENT=%.6f" % high_cur)
        self._run("SET_STEPPER_ENABLE STEPPER=extruder ENABLE=1")

    def cmd_OPEN_MOVE(self, gcmd):
        is_open = int(self._macro("GLOBAL_STATE").get("feeder_open", 0))
        if is_open == 1:
            return

        base_cur = float(self._macro("GLOBAL_STATE").get("e_cur", 0.0))
        old_accel = float(self._status("toolhead").get("max_accel", 0.0))
        new_accel = self._tool_cfg("fast_accel", old_accel)
        e_open = self._tool_cfg("e_open", -4.0)

        self._run("SET_STEPPER_ENABLE STEPPER=extruder ENABLE=1")
        self._run("SET_VELOCITY_LIMIT ACCEL=%.6f" % new_accel)
        self._run("SET_SERVO SERVO=my_servo ANGLE=180")
        self._run("G91")
        self._run("G4 P200")
        self._run("G1 E-0.2 F2500")
        self._run("G1 E0.2 F2500")
        self._run("G1 E-0.2 F2500")
        self._run("G1 E0.2 F2500")
        self._run("G1 E%.6f F2500" % e_open)
        self._run("G90")
        self._run("SET_VELOCITY_LIMIT ACCEL=%.6f" % old_accel)
        self._run("SET_TMC_CURRENT STEPPER=extruder CURRENT=%.6f" % base_cur)
        self._set_var("GLOBAL_STATE", "feeder_open", 1)

    def cmd_SET(self, gcmd):
        t = self._require_t(gcmd, "T")
        self._validate_t(gcmd, t, "SET")

        ct = self._pin_watch()
        self._set_var("GLOBAL_STATE", "error_state", 0)
        self._set_var("GLOBAL_STATE", "target_tool", t)
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

        if ct == -2:
            self._respond("SET: sensor error")
            self.cmd_ERROR(gcmd)
            return
        if ct == t:
            self._respond("SET: already T%d" % t)
            return
        if ct >= 0 and ct < self._max_tool() and ct != t:
            self.cmd_SET_CHANGE_CHECK(self._cmd_t("SET_CHANGE_CHECK", t))
            return

        self.cmd_SET_CHECK(self._cmd_t("SET_CHECK", t))

    def cmd_SET_CHANGE_CHECK(self, gcmd):
        t = self._require_t(gcmd, "T")
        self._validate_t(gcmd, t, "SET_CHANGE_CHECK")
        self.cmd_DROP(gcmd)
        self.cmd_SET_CHECK(self._cmd_t("SET_CHECK", t))

    def cmd_SET_CHECK(self, gcmd):
        if int(self._macro("GLOBAL_STATE").get("error_state", 0)) == 1:
            self._respond("SET_CHECK: paused after DROP")
            return

        t = self._require_t(gcmd, "T")
        self._validate_t(gcmd, t, "SET_CHECK")

        ct = self._pin_watch()
        max_tool = self._max_tool()

        if ct == -2:
            self._respond("SET_CHECK: sensor error")
            self.cmd_ERROR(gcmd)
            return
        if ct == t:
            self._run("TOOL_OFFSET_T T=%d" % t)
            self._respond("SET_CHECK: tool already installed")
            return
        if ct >= 0 and ct < max_tool and ct != t:
            self._respond("SET_CHECK: wrong tool (need change)")
            self.cmd_ERROR(gcmd)
            return
        if ct == -1:
            self.cmd_SET_MOVE(gcmd)
            return

        self._respond("SET_CHECK: invalid current_tool")
        self.cmd_ERROR(gcmd)

    def cmd_SET_MOVE(self, gcmd):
        t = self._require_t(gcmd, "T")
        self._validate_t(gcmd, t, "SET_MOVE")

        y_safe = self._tool_cfg("y_safe")
        y_latch = self._tool_cfg("y_latch")
        y_prime = self._tool_cfg("y_prime")
        x_prime_shift = self._tool_cfg("x_prime_shift")
        x_shift = self._tool_cfg("x_shift")

        x_base = self._tool_cfg("x_t%d" % t)

        fo = int(self._macro("GLOBAL_STATE").get("feeder_open", 0))
        old_accel = float(self._status("toolhead").get("max_accel", 0.0))
        new_accel = self._tool_cfg("fast_accel", old_accel)
        speed = self._tool_cfg("fast_speed", 300.0)
        feedrate = speed * 60.0
        slow_feedrate = speed * 5.0

        if fo != 1:
            self.cmd_OPEN(gcmd)

        self._run("TOOL_OFFSET_T T=0")
        self._run("SET_GCODE_OFFSET X=0 Y=0 MOVE=1")
        self._run("SET_VELOCITY_LIMIT ACCEL=%.6f" % new_accel)
        self._run("G90")
        self._run("G1 Y%.6f X%.6f F%.6f" % (y_safe, x_base, feedrate))
        self._run("G1 Y%.6f F%.6f" % (y_latch + 3.0, feedrate))
        self._run("G1 Y%.6f F%.6f" % (y_latch, slow_feedrate))
        self._run("G1 Y%.6f F%.6f" % (y_latch - 0.3, slow_feedrate))
        self._run("G1 X%.6f F%.6f" % (x_base - x_shift + 2.0, feedrate))
        self._run("G1 X%.6f F%.6f" % (x_base - x_shift, slow_feedrate))
        self._run("G1 Y%.6f F%.6f" % (y_prime, feedrate))
        self._run("G1 X%.6f F%.6f" % (x_base - x_prime_shift, feedrate))
        self._run("SET_VELOCITY_LIMIT ACCEL=%.6f" % old_accel)
        self._run("M106 S255")
        self._run("G4 P1000")

        self.cmd_VERIFY_SET(gcmd)

    def cmd_VERIFY_SET(self, gcmd):
        t = self._require_t(gcmd, "T")
        self._validate_t(gcmd, t, "VERIFY_SET")

        y_safe = self._tool_cfg("y_safe")
        y_brush = self._tool_cfg("y_brush")
        speed = self._tool_cfg("fast_speed", 300.0)
        feedrate = speed * 60.0

        ct = self._pin_watch()
        extr = "extruder" if t == 0 else "extruder%d" % t
        extr_obj = self.printer.lookup_object(extr, None)
        temp = float(getattr(extr_obj, "last_temp", 0.0)) if extr_obj is not None else 0.0
        state = self._tool_state(ct if ct >= 0 else t)

        if ct != t:
            self._respond("VERIFY_SET: mismatch")
            self.cmd_ERROR(gcmd)
            return

        self._respond("VERIFY_SET OK")
        self.cmd_CLOSE(gcmd)
        self._run("G4 P200")

        if self._print_state() == "printing" and temp > 170.0:
            fp_flag = int(state.get("first_prime_flag", 0))
            fp_amt = float(state.get("first_prime_amount", 0.0))
            fp_spd = float(state.get("first_prime_speed", 0.0))
            fp_f = int(fp_spd * 60.0)

            if fp_flag == 0:
                self._respond("FIRST PRIME: executed")
                self._run("G91")
                self._run("G1 E%.6f F%d" % (fp_amt, fp_f))
                self._run("G90")
                self._run("G90")
                self._run("G1 Y%.6f F%.6f" % (y_safe, feedrate))
                self._run("G1 Y%.6f F%.6f" % (self._tool_cfg("y_prime"), feedrate))
                self._set_var("TOOL_STATE_%d" % ct, "first_prime_flag", 1)
            else:
                self._respond("FIRST PRIME: skipped")

            amt = float(state.get("prime_amount", 0.0))
            spd = float(state.get("prime_speed", 0.0))
            r_amt = float(state.get("prime_retract", 0.0))
            r_spd = float(state.get("prime_retract_speed", 0.0))

            e1 = amt * 0.20
            e2 = amt * 0.30
            e3 = amt * 0.50

            f1 = int(spd * 0.50 * 60.0)
            f2 = int(spd * 0.75 * 60.0)
            f3 = int(spd * 1.00 * 60.0)
            fr = int(r_spd * 60.0)

            self._run("G91")
            self._run("G1 E%.6f F%d" % (e1, f1))
            self._run("G1 E%.6f F%d" % (e2, f2))
            self._run("G1 E%.6f F%d" % (e3, f3))
            self._run("G1 E-%.6f F%d" % (r_amt, fr))
            self._run("G90")
            self._respond("PRIME: T executed")

        if self._print_state() == "printing":
            cmx = float(state.get("clean_move_x", 0.0))
            cmy = float(state.get("clean_move_y", 0.0))
            cms = int(float(state.get("clean_move_speed", 0.0)))
            cmf = int(cms * 60.0)

            r_amt = float(state.get("clean_retract", 0.0))
            r_spd = float(state.get("clean_retract_speed", 0.0))
            rf = int(r_spd * 60.0)

            self._run("G1 Y%.6f F%.6f" % (y_brush, feedrate))
            self._run("G91")
            self._run("G1 X%.6f Y%.6f F%d" % (cmx, cmy, cmf))
            self._run("G90")
            self._run("G1 Y%.6f F%.6f" % (y_safe, feedrate))
            self._run("TOOL_OFFSET_T T=%d" % ct)
            self._run("G91")
            self._run("G1 E-%.6f F%d" % (r_amt, rf))
            self._run("G90")
            self._run("G1 F%.6f" % feedrate)
        else:
            self._run("G1 Y%.6f F%.6f" % (y_safe, feedrate))
            self._run("TOOL_OFFSET_T T=%d" % ct)
            self._run("G90")
        self._run("M106 S0")

    def cmd_DROP(self, gcmd):
        self._home_request()
        self.cmd_DROP_CHECK(gcmd)

    def cmd_DROP_CHECK(self, gcmd):
        ct = self._pin_watch()
        max_tool = self._max_tool()

        if ct == -2:
            self._respond("DROP_CHECK: sensor error")
            self.cmd_ERROR(gcmd)
            return
        if ct == -1:
            self._respond("DROP_CHECK: nothing installed")
            return
        if ct < 0 or ct >= max_tool:
            self._respond("DROP_CHECK: invalid current_tool")
            self.cmd_ERROR(gcmd)
            return

        self._respond("DROP_CHECK: T%d" % ct)
        sub = self._cmd_t("DROP_MOVE", ct)
        self.cmd_DROP_MOVE(sub)

    def cmd_DROP_MOVE(self, gcmd):
        t = self._require_t(gcmd, "T")
        self._validate_t(gcmd, t, "DROP_MOVE")

        y_safe = self._tool_cfg("y_safe")
        y_latch = self._tool_cfg("y_latch")
        x_shift = self._tool_cfg("x_shift")
        y_prime = self._tool_cfg("y_prime")
        x_prime_shift = self._tool_cfg("x_prime_shift")

        x_base = self._tool_cfg("x_t%d" % t)

        fo = int(self._macro("GLOBAL_STATE").get("feeder_open", 0))
        old_accel = float(self._status("toolhead").get("max_accel", 0.0))
        new_accel = self._tool_cfg("fast_accel", old_accel)
        speed = self._tool_cfg("fast_speed", 300.0)
        feedrate = speed * 60.0

        self._run("TOOL_OFFSET_T T=0")
        self._run("SET_GCODE_OFFSET X=0 Y=0 MOVE=1")
        self._run("SET_VELOCITY_LIMIT ACCEL=%.6f" % new_accel)
        self._run("G90")
        self._run("G1 Y%.6f X%.6f F%.6f" % (y_safe, x_base - x_prime_shift, feedrate))
        self._run("G1 Y%.6f F%.6f" % (y_prime, feedrate))

        if fo != 1:
            self.cmd_OPEN(gcmd)

        self._run("G90")
        self._run("G1 X%.6f F%.6f" % (x_base - x_shift, feedrate))
        self._run("G1 Y%.6f F%.6f" % (y_latch, feedrate))
        self._run("G1 X%.6f F%.6f" % (x_base, feedrate))
        self._run("G1 Y%.6f F%.6f" % (y_safe, feedrate))
        self._run("SET_VELOCITY_LIMIT ACCEL=%.6f" % old_accel)
        self._run("G4 P900")
        self.cmd_VERIFY_DROP(gcmd)

    def cmd_VERIFY_DROP(self, gcmd):
        ct = self._pin_watch()
        if ct != -1:
            self._respond("VERIFY_DROP: mismatch")
            self.cmd_ERROR(gcmd)
            return
        self._respond("VERIFY_DROP OK")

    def cmd_DROP_CLOSE(self, gcmd):
        self._run("SAVE_GCODE_STATE")
        self._respond("DROP and CLOSE")
        self.cmd_DROP(gcmd)
        self.cmd_CLOSE(gcmd)

    def cmd_CLEAN(self, gcmd):
        ct = self._pin_watch()
        max_tool = self._max_tool()

        if ct < 0 or ct >= max_tool:
            self._respond("CLEAN: no valid tool selected")
            self.cmd_ERROR(gcmd)
            return

        self._home_request()

        y_safe = self._tool_cfg("y_safe")
        y_brush = self._tool_cfg("y_brush")
        y_prime = self._tool_cfg("y_prime")
        x_prime_shift = self._tool_cfg("x_prime_shift")
        old_accel = float(self._status("toolhead").get("max_accel", 0.0))
        new_accel = self._tool_cfg("fast_accel", old_accel)
        speed = self._tool_cfg("fast_speed", 300.0)
        feedrate = speed * 60.0
        feedrate_clean = speed * 10.0
        x_base = self._tool_cfg("x_t%d" % ct)

        self._run("SET_VELOCITY_LIMIT ACCEL=%.6f" % new_accel)
        self._run("G90")
        self._run("G1 Y%.6f F%.6f" % (y_safe, feedrate))
        self._run("G1 X%.6f F%.6f" % (x_base, feedrate))
        self._run("G1 Y%.6f F%.6f" % (y_prime, feedrate))
        self._run("G1 X%.6f F%.6f" % (x_base - x_prime_shift, feedrate))
        self._run("G1 Y%.6f F%.6f" % (y_brush, feedrate))

        for _ in range(3):
            self._run("G1 X%.6f F%.6f" % (x_base - x_prime_shift + 10.0, feedrate_clean))
            self._run("G1 X%.6f F%.6f" % (x_base - x_prime_shift, feedrate_clean))

        self._run("G1 X%.6f F%.6f" % (x_base, feedrate))
        self._run("G1 Y%.6f F%.6f" % (y_safe, feedrate))
        self._run("SET_VELOCITY_LIMIT ACCEL=%.6f" % old_accel)

    def cmd_ERROR(self, gcmd):
        y_safe = self._tool_cfg("y_safe")
        target_tool = int(self._macro("GLOBAL_STATE").get("target_tool", -1))
        state = self._print_state()

        if state in ("printing", "paused"):
            self._respond("ERROR")
            self._set_var("GLOBAL_STATE", "error_state", 1)
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

        st = self._tool_offset_state()
        x = float(st.get("t%d_off_x" % t, 0.0))
        y = float(st.get("t%d_off_y" % t, 0.0))
        z_off = float(st.get("t%d_off_z" % t, 0.0))

        z_eddy = float(self._macro("GLOBAL_STATE").get("eddy_z", 0.0))
        z = z_off + z_eddy

        self._run("SET_GCODE_OFFSET X=%.6f Y=%.6f Z=%.6f MOVE=%d" % (x, y, z, move))
        self._respond("TOOL_OFFSET_T: X=%.6f Y=%.6f Z=%.6f MOVE=%d" % (x, y, z, move))

    def cmd_ASSIGN_TCH_TOOL(self, gcmd):
        ct = self._pin_watch()
        if ct >= 0:
            self._run("INITIALIZE_TOOLCHANGER T=%d" % ct)
            self._respond("ASSIGN_TOOL: INITIALIZE_TOOLCHANGER T=%d" % ct)
        else:
            self._respond("No tool installed. Initialization after next change.")

    def cmd_LAYER_SET(self, gcmd):
        try:
            l = int(gcmd.get_int("L"))
        except Exception:
            return
        self._set_var("GLOBAL_STATE", "layer", l)

    def cmd_PRIME_FLAGS_CLEAR(self, gcmd):
        max_tool = self._max_tool()
        for t in range(max_tool):
            self._set_var("TOOL_STATE_%d" % t, "first_prime_flag", 0)
        self._respond("PRIME FLAG CLEAR")

    def cmd_PRIME_FLAGS_SET(self, gcmd):
        max_tool = self._max_tool()
        for t in range(max_tool):
            self._set_var("TOOL_STATE_%d" % t, "first_prime_flag", 1)
        self._respond("PRIME FLAG SET")


def load_config(config):
    return MedusaHC(config)
