import json
import os
import tempfile
import unittest
from pathlib import Path

from molcajete_player.contract import ContractEnv
from molcajete_player.controller import HEARTBEAT_SECONDS, POLL_INTERVAL_MS, ContractController

STREAM = "https://stream.mux.com/abc.m3u8"


class FakePlayer:
    """Apunta lo que le piden los ficheros del contrato; su estado de reproduccion lo fija cada test."""

    def __init__(self):
        self.loaded = []
        self.calls = []
        self.state = None

    def load_playlist(self, playlist):
        self.loaded.append(playlist)
        self.state = "playing"

    def pause(self):
        self.calls.append("pause")
        if self.state:
            self.state = "paused"

    def play(self):
        self.calls.append("play")
        if self.state:
            self.state = "playing"

    def playback_state(self):
        return self.state


def _program(program_hash="h1", clips=None):
    clips = clips if clips is not None else [{"clip_id": 1, "title": "A", "stream_url": STREAM}]
    return {"program_hash": program_hash, "manifest": {"version": 2, "clips": clips}}


class TestContractController(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = ContractEnv(app_data=Path(self.tmp.name), video_output="fakesink")
        self.player = FakePlayer()
        self.now = 0.0
        self.controller = ContractController(self.env, self.player, clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, path, document):
        path.write_text(json.dumps(document))
        # Asegura que la firma (fecha, tamano) cambia aunque el test escriba dos veces en el mismo instante.
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    def _status(self):
        return json.loads(self.env.status_path.read_text())

    def test_start_applies_the_mailbox_and_schedules_checks(self):
        self._write(self.env.program_path, _program())
        scheduled = []

        self.assertFalse(self.controller.start(lambda ms, fn: scheduled.append((ms, fn))))

        self.assertEqual(scheduled, [(POLL_INTERVAL_MS, self.controller.tick)])
        self.assertEqual(len(self.player.loaded), 1)
        status = self._status()
        self.assertEqual((status["program_hash"], status["error"], status["playback"]), ("h1", None, "playing"))

    def test_without_a_program_it_says_so(self):
        self.controller.tick()
        self.assertEqual(self._status()["program_hash"], None)
        self.assertEqual(self.player.loaded, [])

    def test_a_new_program_is_loaded_without_restarting(self):
        self._write(self.env.program_path, _program("h1"))
        self.controller.tick()
        self._write(self.env.program_path, _program("h2"))
        self.controller.tick()

        self.assertEqual([p.name for p in self.player.loaded], ["h1", "h2"])
        self.assertEqual(self._status()["program_hash"], "h2")

    def test_the_same_program_rewritten_does_not_cut_the_video(self):
        self._write(self.env.program_path, _program("h1"))
        self.controller.tick()
        self._write(self.env.program_path, _program("h1"))
        self.controller.tick()

        self.assertEqual(len(self.player.loaded), 1)

    def test_a_rejected_program_keeps_the_previous_one_and_says_why(self):
        self._write(self.env.program_path, _program("h1"))
        self.controller.tick()
        self._write(self.env.program_path, _program("h2", clips=[{"clip_id": 1, "stream_url": "file:///etc/passwd"}]))
        self.controller.tick()

        self.assertEqual(len(self.player.loaded), 1)
        status = self._status()
        self.assertEqual(status["program_hash"], "h1")
        self.assertEqual(status["error"]["program_hash"], "h2")
        self.assertIn("stream_url", status["error"]["reason"])

    def test_an_unreadable_program_is_reported(self):
        self.env.program_path.write_text("{not json")
        self.controller.tick()
        status = self._status()
        self.assertIsNone(status["error"]["program_hash"])
        self.assertIn("unreadable", status["error"]["reason"])

    def test_accepting_a_program_clears_the_previous_rejection(self):
        self._write(self.env.program_path, _program("h2", clips="no"))
        self.controller.tick()
        self._write(self.env.program_path, _program("h3"))
        self.controller.tick()
        self.assertEqual((self._status()["program_hash"], self._status()["error"]), ("h3", None))

    def test_control_pauses_and_resumes_and_status_reflects_it(self):
        self._write(self.env.program_path, _program())
        self.controller.tick()

        self._write(self.env.control_path, {"playback": "paused"})
        self.controller.tick()
        self.assertEqual(self._status()["playback"], "paused")

        self._write(self.env.control_path, {"playback": "playing"})
        self.controller.tick()
        self.assertEqual(self._status()["playback"], "playing")
        self.assertEqual(self.player.calls, ["play", "pause", "play"])

    def test_a_pause_requested_before_the_program_is_applied_first(self):
        self._write(self.env.control_path, {"playback": "paused"})
        self._write(self.env.program_path, _program())
        self.controller.tick()
        self.assertEqual(self.player.calls, ["pause"])

    def test_status_is_only_rewritten_on_change_or_as_a_heartbeat(self):
        self.controller.tick()
        first = self.env.status_path.stat().st_mtime_ns
        self.env.status_path.write_text("tocado")
        self.controller.tick()
        self.assertEqual(self.env.status_path.read_text(), "tocado")

        self.now += HEARTBEAT_SECONDS
        self.controller.tick()
        self.assertNotEqual(self.env.status_path.read_text(), "tocado")
        self.assertGreaterEqual(self.env.status_path.stat().st_mtime_ns, first)


if __name__ == "__main__":
    unittest.main()
