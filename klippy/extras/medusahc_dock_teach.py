"""Measure MedusaHC dock XY by hand-placement after M84.

Workflow:
  1. M84 (keep the head clear of the bed by hand if needed)
  2. Push the toolhead to the dock by hand
  3. TEACH_DOCK TOOL=n

Uses ``_TOOL_CFG.tools_direction`` (1 = front docks, -1 = rear docks):
  - Rear: home Y toward the front endstop (leaves the rack).
  - Front: home Y toward the back (leaves the rack).

Then homes X. Coordinates are inferred from stepper MCU travel from the
hand-placed pose and printed to the console.
"""

import logging

from .homing import Homing


class MedusaHCDockTeach:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.home_current_ratio = config.getfloat(
            "home_current_ratio", 0.7, above=0.0, maxval=1.0
        )
        self.bump_distance = config.getfloat("bump_distance", 10.0, above=0.0)
        self.bump_speed = config.getfloat("bump_speed", 20.0, above=0.0)
        self.verbose = config.getboolean("verbose", True)
        self._tmc_names = ("tmc2209 stepper_x", "tmc2209 stepper_y")
        self.gcode.register_command(
            "MHC_TEACH_DOCK",
            self.cmd_MHC_TEACH_DOCK,
            desc="Hand-place at dock, home Y then X, print measured dock X/Y",
        )

    def _respond(self, msg):
        self.gcode.respond_info(msg)

    def _toolhead(self):
        return self.printer.lookup_object("toolhead")

    def _kin(self):
        return self._toolhead().get_kinematics()

    def _tool_cfg(self):
        for name in ("_TOOL_CFG", "TOOL_CFG"):
            obj = self.printer.lookup_object("gcode_macro %s" % name, None)
            if obj is not None:
                return obj.variables
        raise self.printer.command_error(
            "TEACH_DOCK: requires [gcode_macro _TOOL_CFG] or [gcode_macro TOOL_CFG]"
        )

    def _tools_direction(self):
        direction = int(self._tool_cfg().get("tools_direction", 1))
        if direction not in (-1, 1):
            raise self.printer.command_error(
                "TEACH_DOCK: TOOL_CFG.tools_direction must be 1 (front) or -1 (rear)"
            )
        return direction

    def _check_idle(self, gcmd):
        stats = self.printer.lookup_object("print_stats", None)
        if stats is None:
            return
        state = stats.get_status(self.reactor.monotonic()).get("state", "")
        if state in ("printing", "paused"):
            raise gcmd.error(
                "TEACH_DOCK: refuse while print is %s" % state
            )

    def _mcu_snapshot(self, kin):
        return {s.get_name(): s.get_mcu_position() for s in kin.get_steppers()}

    def _cart_travel(self, kin, mcu_before, mcu_after):
        """Cartesian travel from MCU step deltas (valid even after set_position)."""
        delta_spos = {}
        for stepper in kin.get_steppers():
            name = stepper.get_name()
            delta_spos[name] = (
                (mcu_after[name] - mcu_before[name]) * stepper.get_step_dist()
            )
        return kin.calc_position(delta_spos)

    def _rail(self, kin, axis):
        rails = getattr(kin, "rails", None)
        if rails is None or axis >= len(rails):
            raise self.printer.command_error(
                "TEACH_DOCK: kinematics do not expose rail %s" % "xy"[axis]
            )
        return rails[axis]

    def _rail_range(self, kin, axis):
        return self._rail(kin, axis).get_range()

    def _configured_endstop(self, kin, axis):
        return float(self._rail(kin, axis).get_homing_info().position_endstop)

    def _configured_home_sign(self, kin, axis):
        """Sign of the rail's configured homing move: -1 toward min, +1 toward max."""
        return 1 if self._rail(kin, axis).get_homing_info().positive_dir else -1

    def _get_run_currents(self):
        currents = {}
        for name in self._tmc_names:
            tmc = self.printer.lookup_object(name, None)
            if tmc is None:
                raise self.printer.command_error(
                    "TEACH_DOCK: missing [%s] for sensorless home current"
                    % name
                )
            status = tmc.get_status(self.reactor.monotonic())
            currents[name] = float(status["run_current"])
        return currents

    def _set_home_currents(self, run_currents):
        for name, current in run_currents.items():
            stepper = name.split(" ", 1)[1]
            home_current = round(current * self.home_current_ratio, 2)
            self.gcode.run_script_from_command(
                "SET_TMC_CURRENT STEPPER=%s CURRENT=%.2f"
                % (stepper, home_current)
            )

    def _restore_currents(self, run_currents):
        for name, current in run_currents.items():
            stepper = name.split(" ", 1)[1]
            self.gcode.run_script_from_command(
                "SET_TMC_CURRENT STEPPER=%s CURRENT=%.3f"
                % (stepper, current)
            )

    def _move_axis_relative(self, axis, distance, speed=None):
        if speed is None:
            speed = self.bump_speed
        toolhead = self._toolhead()
        dest = list(toolhead.get_position())
        dest[axis] += distance
        toolhead.move(dest, speed)
        toolhead.wait_moves()

    def _retract_off_home(self, axis, home_sign):
        """Step away from the just-triggered home end (opposite of home_sign)."""
        self._move_axis_relative(axis, -home_sign * self.bump_distance)
        self._toolhead().dwell(0.35)

    def _home_rail_to(self, axis, target_pos, home_sign):
        """Sensorless-home one axis toward target_pos (min or max end of travel)."""
        toolhead = self._toolhead()
        kin = self._kin()
        rail = self._rail(kin, axis)
        position_min, position_max = rail.get_range()
        span = position_max - position_min

        homepos = [None, None, None, None]
        homepos[axis] = target_pos
        forcepos = list(homepos)
        if home_sign > 0:
            # Moving toward max: pretend we start past min.
            forcepos[axis] = position_min - 1.5 * span
        else:
            # Moving toward min: pretend we start past max.
            forcepos[axis] = position_max + 1.5 * span

        homing_state = Homing(self.printer)
        homing_state.set_axes([axis])
        homing_state.home_rails([rail], forcepos, homepos)
        toolhead.wait_moves()

    def _home_y_for_rack(self, tools_direction):
        """Home Y away from the rack. Returns (home_sign, assigned_end_position)."""
        kin = self._kin()
        position_min, position_max = self._rail_range(kin, 1)
        configured_sign = self._configured_home_sign(kin, 1)
        configured_end = self._configured_endstop(kin, 1)

        if tools_direction < 0:
            # Rear rack: leave toward the front = configured min home (typical).
            self._respond("TEACH_DOCK: rear rack — homing Y toward front")
            self._home_rail_to(1, configured_end, configured_sign)
            return configured_sign, configured_end

        # Front rack: leave toward the back = opposite end of travel.
        back_pos = position_max if configured_sign < 0 else position_min
        back_sign = -configured_sign
        self._respond("TEACH_DOCK: front rack — homing Y toward back")
        self._home_rail_to(1, back_pos, back_sign)
        return back_sign, back_pos

    def _dock_from_travel(self, axis, mcu_before, mcu_after, end_position):
        kin = self._kin()
        travel = self._cart_travel(kin, mcu_before, mcu_after)
        dock = end_position - travel[axis]
        if self.verbose:
            self._respond(
                "TEACH_DOCK: axis %s travel=%.3f end=%.3f -> dock=%.3f"
                % ("xy"[axis], travel[axis], end_position, dock)
            )
        return dock, travel

    def cmd_MHC_TEACH_DOCK(self, gcmd):
        tool = gcmd.get_int("TOOL", default=None)
        if tool is None:
            tool = gcmd.get_int("T")
        self._check_idle(gcmd)
        tools_direction = self._tools_direction()

        self._respond(
            "TEACH_DOCK T%d: snapshot dock pose, home Y then X (tools_direction=%d)"
            % (tool, tools_direction)
        )

        kin = self._kin()
        mcu_dock = self._mcu_snapshot(kin)

        run_currents = self._get_run_currents()
        try:
            self._set_home_currents(run_currents)

            y_home_sign, y_end = self._home_y_for_rack(tools_direction)
            dock_y, travel_y = self._dock_from_travel(
                1, mcu_dock, self._mcu_snapshot(kin), y_end
            )
            self._retract_off_home(1, y_home_sign)

            # X always uses the configured endstop.
            x_sign = self._configured_home_sign(kin, 0)
            x_end = self._configured_endstop(kin, 0)
            self._home_rail_to(0, x_end, x_sign)
            dock_x, travel_x = self._dock_from_travel(
                0, mcu_dock, self._mcu_snapshot(kin), x_end
            )

            if abs(travel_y[1]) < 50.0:
                self._respond(
                    "TEACH_DOCK: WARNING Y travel only %.1fmm — re-place and retry"
                    % travel_y[1]
                )
            if abs(travel_x[0]) < 20.0:
                self._respond(
                    "TEACH_DOCK: WARNING X travel only %.1fmm — sensorless may have "
                    "triggered early; re-place and retry" % travel_x[0]
                )
        except self.printer.command_error:
            logging.exception("TEACH_DOCK failed")
            try:
                self.printer.lookup_object("stepper_enable").motor_off()
            except Exception:
                pass
            raise
        finally:
            try:
                self._restore_currents(run_currents)
            except Exception:
                logging.exception("TEACH_DOCK: failed to restore TMC currents")

        self._respond(
            "TEACH_DOCK T%d: x=%.3f y=%.3f"
            % (tool, dock_x, dock_y)
        )


def load_config(config):
    return MedusaHCDockTeach(config)
