import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from test_medusahc_calibrate import MODULE


class CalibrationEntryTests(unittest.TestCase):
    def make_controller(self, print_state="standby"):
        controller = object.__new__(MODULE.MedusaHCCalibrate)
        controller.operation = "idle"
        controller.printer = Mock()
        controller.printer.lookup_object.return_value = SimpleNamespace(state=print_state)
        controller._run = Mock()
        controller._move = Mock()
        controller._emergency_lift = Mock()
        return controller

    def test_print_refusal_has_no_side_effects_for_all_methods(self):
        for state in ("printing", "paused"):
            for method in ("touch", "xyz", "z"):
                with self.subTest(state=state, method=method):
                    controller = self.make_controller(state)
                    command = SimpleNamespace(error=RuntimeError)
                    with self.assertRaisesRegex(RuntimeError, "during a print"):
                        if method == "touch":
                            controller.cmd_MHC_CALIBRATE_ALL(command)
                        else:
                            controller._run_eddy_calibration(command, method == "xyz")
                    controller._run.assert_not_called()
                    controller._move.assert_not_called()
                    controller._emergency_lift.assert_not_called()
                    self.assertEqual(controller.operation, "idle")

    def test_missing_coil_coordinates_refuse_before_heating(self):
        controller = self.make_controller()
        controller.eddy_seek_x = controller.eddy_seek_y = None
        with self.assertRaisesRegex(RuntimeError, "eddy_seek_x"):
            controller._run_eddy_calibration(SimpleNamespace(error=RuntimeError), True)
        controller._run.assert_not_called()
        controller._emergency_lift.assert_not_called()

    def test_seek_positions_t0_and_later_tools_before_measurement(self):
        for tool in (0, 2):
            controller = self.make_controller()
            controller.eddy_seek_x, controller.eddy_seek_y = 120, 210
            controller.eddy_seek_z, controller.final_lift_z = 5, 6
            controller.positioning_speed = 100
            controller.transition_speed = 200
            controller.eddy_seek_repeats = 3
            eddy = Mock()
            eddy._tools.get_tool.side_effect = [None, SimpleNamespace(offset=SimpleNamespace(x=1, y=2))]
            head = Mock()
            head.get_position.return_value = [0, 0, 8, 0]
            controller.printer.lookup_object.side_effect = lambda name, *args: head if name == "toolhead" else eddy
            controller.gcode = Mock()
            controller._wait_moves = Mock()
            events = []
            controller._move.side_effect = lambda point, speed: events.append(point)
            controller._run.side_effect = lambda script: events.append(script)
            self.assertEqual(controller._eddy_seek_xy(tool, .2), (1, 2))
            self.assertEqual(events[:4], [[None, None, 8], [120, None, None], [None, 210, None], [None, None, 5.2]])
            self.assertIn("TOOL=%d" % tool, events[4])
