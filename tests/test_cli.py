import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from molcajete_player.main import build_parser, main


class TestCLIAndRunScript(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).resolve().parent.parent
        self.run_script = self.repo_root / "run.sh"
        self.sample_path = str(self.repo_root / "examples" / "sample_playlist.json")
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name) / "kiosk_cache"
        self.cache_dir.mkdir()
        # Nunca tocar la cache real del desarrollador desde las pruebas.
        self.env = dict(os.environ, MOLCAJETE_CACHE_DIR=str(self.cache_dir))

    def tearDown(self):
        self._tmp.cleanup()

    def run_main(self, *argv):
        with patch("sys.argv", ["molcajete-player", *argv]), \
                patch.dict(os.environ, {"MOLCAJETE_CACHE_DIR": str(self.cache_dir)}):
            return main()

    def test_build_parser_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["examples/sample_playlist.json"])
        self.assertEqual(args.metadata, "examples/sample_playlist.json")
        self.assertEqual(args.sink, "auto")
        self.assertIsNone(args.cache_dir)
        self.assertIsNone(args.loop)
        self.assertIsNone(args.media_root)
        self.assertFalse(args.clean_cache)
        self.assertTrue(args.prune_cache)
        self.assertTrue(args.prefetch_next)
        self.assertFalse(args.allow_insecure_http)
        self.assertFalse(args.verbose)

    def test_build_parser_custom_options(self):
        parser = build_parser()
        args = parser.parse_args([
            "examples/sample_playlist.json",
            "--cache-dir", "/var/cache/kiosk",
            "--no-loop",
            "--no-prune-cache",
            "--media-root", "/srv/media",
            "--clean-cache",
        ])
        self.assertEqual(args.cache_dir, "/var/cache/kiosk")
        self.assertFalse(args.loop)
        self.assertFalse(args.prune_cache)
        self.assertEqual(args.media_root, "/srv/media")
        self.assertTrue(args.clean_cache)

    def test_run_sh_help_works(self):
        self.assertTrue(self.run_script.is_file(), "El script run.sh debe existir en la raiz.")
        result = subprocess.run(
            [str(self.run_script), "--help"],
            capture_output=True,
            text=True,
            check=True,
            env=self.env,
        )
        self.assertIn("molcajete-player", result.stdout)
        self.assertIn("--sink", result.stdout)
        self.assertIn("--cache-dir", result.stdout)
        self.assertIn("--clean-cache", result.stdout)

    def test_run_sh_does_not_delete_cache_dir(self):
        cached = self.cache_dir / "clip-001_0123456789abcdef.mp4"
        cached.write_bytes(b"cached-video")
        subprocess.run([str(self.run_script), "--help"], capture_output=True, text=True, check=True, env=self.env)
        self.assertTrue(cached.exists())

        other_dir = Path(self._tmp.name) / "datos"
        other_dir.mkdir()
        (other_dir / "importante.txt").write_text("no borrar")
        subprocess.run(
            [str(self.run_script), "--cache-dir", str(other_dir), "--help"],
            capture_output=True,
            text=True,
            check=True,
            env=self.env,
        )
        self.assertTrue((other_dir / "importante.txt").exists())

    @patch("molcajete_player.main.MolcajetePlayer")
    def test_main_with_default_args_succeeds(self, mock_player_cls):
        mock_instance = MagicMock()
        mock_instance.has_fatal_error = False
        mock_player_cls.return_value = mock_instance

        exit_code = self.run_main(self.sample_path, "--sink", "auto")

        self.assertEqual(exit_code, 0)
        mock_player_cls.assert_called_once()
        mock_instance.run.assert_called_once()
        self.assertTrue(mock_player_cls.call_args.kwargs["prune_cache"])
        self.assertNotIn("audio", mock_player_cls.call_args.kwargs)
        self.assertTrue(mock_player_cls.call_args.kwargs["prefetch_next"])

    @patch("molcajete_player.main.MolcajetePlayer")
    def test_main_requires_https_downloads_by_default(self, mock_player_cls):
        mock_player_cls.return_value.has_fatal_error = False
        self.run_main(self.sample_path)
        self.assertFalse(mock_player_cls.call_args.kwargs["downloader"].allow_insecure_http)

    @patch("molcajete_player.main.MolcajetePlayer")
    def test_main_allow_insecure_http_reaches_the_downloader(self, mock_player_cls):
        mock_player_cls.return_value.has_fatal_error = False
        self.run_main(self.sample_path, "--allow-insecure-http")
        self.assertTrue(mock_player_cls.call_args.kwargs["downloader"].allow_insecure_http)

    @patch("molcajete_player.main.MolcajetePlayer")
    def test_main_no_prefetch_flag(self, mock_player_cls):
        mock_player_cls.return_value.has_fatal_error = False
        self.run_main(self.sample_path, "--no-prefetch")
        self.assertFalse(mock_player_cls.call_args.kwargs["prefetch_next"])

    @patch("molcajete_player.main.MolcajetePlayer")
    def test_main_no_loop_overrides_json(self, mock_player_cls):
        mock_player_cls.return_value.has_fatal_error = False
        self.run_main(self.sample_path, "--no-loop")
        playlist = mock_player_cls.call_args.kwargs["playlist"]
        self.assertFalse(playlist.loop)

    @patch("molcajete_player.main.MolcajetePlayer")
    def test_main_clean_cache_removes_only_player_files(self, mock_player_cls):
        mock_player_cls.return_value.has_fatal_error = False
        cached = self.cache_dir / "clip-001_0123456789abcdef.mp4"
        cached.write_bytes(b"x")
        unrelated = self.cache_dir / "notas.txt"
        unrelated.write_text("conservar")

        self.assertEqual(self.run_main(self.sample_path, "--clean-cache"), 0)

        self.assertFalse(cached.exists())
        self.assertTrue(unrelated.exists())

    def test_audio_option_does_not_exist(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            build_parser().parse_args(["examples/sample_playlist.json", "--audio"])

    def test_main_exit_code_1_when_file_missing(self):
        self.assertEqual(self.run_main("/no/existe.json"), 1)

    def test_main_exit_code_1_when_json_invalid(self):
        bad = Path(self._tmp.name) / "malo.json"
        bad.write_text('{"name": "x", "loop": "quizas"}')
        self.assertEqual(self.run_main(str(bad)), 1)

    def test_main_exit_code_1_when_local_path_outside_media_root(self):
        playlist = Path(self._tmp.name) / "lista.json"
        playlist.write_text('{"name": "x", "items": [{"id": "a", "title": "A", "local_path": "/etc/passwd"}]}')
        self.assertEqual(self.run_main(str(playlist), "--media-root", self._tmp.name), 1)

    @patch("molcajete_player.main.MolcajetePlayer", side_effect=RuntimeError("sin GStreamer"))
    def test_main_exit_code_2_when_player_cannot_start(self, _mock):
        self.assertEqual(self.run_main(self.sample_path), 2)

    @patch("molcajete_player.main.MolcajetePlayer")
    def test_main_exit_code_3_on_unexpected_error(self, mock_player_cls):
        mock_player_cls.return_value.run.side_effect = OSError("boom")
        with self.assertLogs(level="ERROR") as logs:
            self.assertEqual(self.run_main(self.sample_path), 3)
        self.assertIn("Traceback", "\n".join(logs.output))

    @patch("molcajete_player.main.MolcajetePlayer")
    def test_main_exit_code_4_on_fatal_playback_error(self, mock_player_cls):
        mock_player_cls.return_value.has_fatal_error = True
        self.assertEqual(self.run_main(self.sample_path), 4)


if __name__ == "__main__":
    unittest.main()
