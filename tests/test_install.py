import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


BASH = shutil.which("bash") or ("C:/Program Files/Git/bin/bash.exe" if Path("C:/Program Files/Git/bin/bash.exe").is_file() else None)
ROOT = Path(__file__).parents[1]


@unittest.skipUnless(BASH, "Bash is required")
class InstallerTests(unittest.TestCase):
    def test_update_and_uninstall_keep_user_config_and_remove_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            extras = root / "klipper/klippy/extras"
            extras.mkdir(parents=True)
            config = root / "config"
            core = config / "MedusaHC"
            core.mkdir(parents=True)
            for path in (extras / "pin_watch.py", extras / "medusahc.py", core / "MHC_variables.cfg", core / "MHC_config.cfg"):
                path.write_text("# test fixture\n", encoding="utf-8")
            (config / "printer.cfg").write_text("[include MedusaHC/MHC_config.cfg]\n", encoding="utf-8")
            (config / "moonraker.conf").write_text("[server]\n", encoding="utf-8")
            env = {**os.environ, "KLIPPER_DIR": (root / "klipper").as_posix(),
                   "PRINTER_CONFIG_DIR": config.as_posix()}
            def run(action):
                result = subprocess.run([BASH, (ROOT / "install.sh").as_posix(), action], env=env,
                                        input="y\ny\ny\ny\n", capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run("install")
            live = core / "medusahc_calibrate.cfg"
            live.write_text("# calibrated positions\n", encoding="utf-8")
            run("update")
            self.assertEqual(live.read_text(), "# calibrated positions\n")
            run("uninstall")
            self.assertEqual(live.read_text(), "# calibrated positions\n")
            self.assertFalse((extras / "medusahc_calibrate.py").exists())
            self.assertNotIn("medusahc_calibrate.cfg", (core / "MHC_config.cfg").read_text())
            self.assertNotIn("update_manager", (config / "moonraker.conf").read_text())
            self.assertTrue((extras / "medusahc.py").exists())
            run("install")
            self.assertEqual(live.read_text(), "# calibrated positions\n")
