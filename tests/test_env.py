import os
import unittest
from unittest.mock import patch

from molcajete_player.env import sanitize_snap_env


class TestSnapEnvSanitization(unittest.TestCase):
    def test_sanitize_snap_env_removes_snap_paths_and_restores_xdg(self):
        fake_env = {
            "GTK_PATH": "/snap/code/271/usr/lib/x86_64-linux-gnu/gtk-3.0",
            "GIO_MODULE_DIR": "/home/user/snap/code/common/.cache/gio-modules",
            "LD_LIBRARY_PATH": "/snap/code/271/usr/lib",
            "LOCPATH": "/usr/lib/locale",
            "XDG_DATA_DIRS": "/snap/code/271/usr/share:/usr/share",
            "XDG_DATA_DIRS_VSCODE_SNAP_ORIG": "/usr/local/share:/usr/share",
        }
        with patch.dict(os.environ, fake_env, clear=True):
            sanitize_snap_env(platform="linux")
            self.assertNotIn("GTK_PATH", os.environ)
            self.assertNotIn("GIO_MODULE_DIR", os.environ)
            self.assertNotIn("LD_LIBRARY_PATH", os.environ)
            self.assertEqual(os.environ.get("LOCPATH"), "/usr/lib/locale")
            self.assertEqual(os.environ.get("XDG_DATA_DIRS"), "/usr/local/share:/usr/share")

    def test_sanitize_snap_env_is_noop_on_macos(self):
        fake_env = {
            "GTK_PATH": "/opt/homebrew/lib/gtk-3.0",
            "LOCPATH": "/opt/homebrew/share/locale",
        }
        with patch.dict(os.environ, fake_env, clear=True):
            sanitize_snap_env(platform="darwin")
            self.assertEqual(os.environ.get("GTK_PATH"), "/opt/homebrew/lib/gtk-3.0")
            self.assertEqual(os.environ.get("LOCPATH"), "/opt/homebrew/share/locale")

    def test_importing_player_does_not_modify_environment(self):
        import subprocess
        import sys

        env = dict(os.environ, GTK_PATH="/snap/code/271/usr/lib/gtk-3.0")
        result = subprocess.run(
            [sys.executable, "-c", "import os, molcajete_player.player; print(os.environ.get('GTK_PATH'))"],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        self.assertEqual(result.stdout.strip(), "/snap/code/271/usr/lib/gtk-3.0")

    def test_run_sh_and_python_sanitize_the_same_variables(self):
        from pathlib import Path

        from molcajete_player.env import SNAP_POLLUTED_VARS

        run_sh = (Path(__file__).resolve().parent.parent / "run.sh").read_text()
        for var in SNAP_POLLUTED_VARS:
            self.assertIn(var, run_sh)


if __name__ == "__main__":
    unittest.main()
