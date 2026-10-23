import json
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from molcajete_player.contract import (
    ContractEnv,
    ContractError,
    ProgramError,
    program_to_playlist,
    read_control_playback,
    read_json,
    status_document,
    write_json_atomic,
)

STREAM = "https://stream.mux.com/abc123.m3u8"
DOWNLOAD = "https://xyz.supabase.co/storage/v1/object/sign/media-masters/j1/rendition/1080p.mp4?token=t"


def _clip(**overrides):
    clip = {"clip_id": 7, "title": "Verano", "duration_s": 12.04, "stream_url": STREAM, "download_url": DOWNLOAD}
    clip.update(overrides)
    return clip


def _program(*clips, program_hash="hash-1", version=2):
    return {"program_hash": program_hash, "manifest": {"version": version, "clips": list(clips)}}


class TestContractEnv(unittest.TestCase):
    def test_without_the_agent_environment_it_is_command_line_mode(self):
        self.assertIsNone(ContractEnv.from_environ({}))

    def test_reads_the_environment_given_by_the_agent(self):
        env = ContractEnv.from_environ(
            {
                "MOLCAJETE_APP_CONTRACT": "1",
                "MOLCAJETE_APP_DATA": "/data/app/player",
                "MOLCAJETE_VIDEO_OUTPUT": "kms",
                "MOLCAJETE_DEV_INSECURE_HTTP": "1",
            }
        )
        self.assertEqual(env.program_path, Path("/data/app/player/program.json"))
        self.assertEqual(env.control_path, Path("/data/app/player/control.json"))
        self.assertEqual(env.status_path, Path("/data/app/player/status.json"))
        self.assertEqual(env.cache_dir, Path("/data/app/player/cache"))
        self.assertEqual(env.sink_type, "kms")
        self.assertTrue(env.dev_insecure_http)

    def test_the_agent_inbox_can_be_a_separate_folder(self):
        env = ContractEnv.from_environ(
            {"MOLCAJETE_APP_CONTRACT": "1", "MOLCAJETE_APP_DATA": "/data/app/player", "MOLCAJETE_APP_INBOX": "/data/app/player-inbox"}
        )
        self.assertEqual(env.program_path, Path("/data/app/player-inbox/program.json"))
        self.assertEqual(env.control_path, Path("/data/app/player-inbox/control.json"))
        self.assertEqual(env.status_path, Path("/data/app/player/status.json"))
        self.assertEqual(env.cache_dir, Path("/data/app/player/cache"))

    def test_an_unknown_video_output_falls_back_to_auto(self):
        env = ContractEnv.from_environ({"MOLCAJETE_APP_CONTRACT": "1", "MOLCAJETE_APP_DATA": "/d"})
        self.assertEqual(env.sink_type, "auto")
        self.assertFalse(env.dev_insecure_http)

    def test_rejects_a_contract_version_it_does_not_understand(self):
        with self.assertRaisesRegex(ContractError, "contract version"):
            ContractEnv.from_environ({"MOLCAJETE_APP_CONTRACT": "2", "MOLCAJETE_APP_DATA": "/d"})

    def test_requires_the_data_folder(self):
        with self.assertRaisesRegex(ContractError, "MOLCAJETE_APP_DATA"):
            ContractEnv.from_environ({"MOLCAJETE_APP_CONTRACT": "1"})


class TestProgramToPlaylist(unittest.TestCase):
    def test_translates_the_platform_program_into_a_looping_playlist(self):
        program_hash, playlist = program_to_playlist(_program(_clip()))

        self.assertEqual(program_hash, "hash-1")
        self.assertEqual((playlist.name, playlist.loop), ("hash-1", True))
        [item] = playlist.items
        self.assertEqual(
            (item.id, item.title, item.remote_url, item.s3_url, item.duration, item.local_path),
            ("7", "Verano", STREAM, DOWNLOAD, 12.04, None),
        )

    def test_a_clip_without_a_copy_goes_by_stream_only(self):
        [item] = program_to_playlist(_program(_clip(download_url=None)))[1].items
        self.assertEqual((item.remote_url, item.s3_url), (STREAM, None))

    def test_never_takes_a_local_path_or_other_fields_from_the_platform(self):
        clip = _clip(local_path="/etc/shadow", s3_url="https://evil.example/x.mp4", sha256="0" * 64)
        [item] = program_to_playlist(_program(clip))[1].items
        self.assertIsNone(item.local_path)
        self.assertEqual(item.s3_url, DOWNLOAD)
        self.assertIsNone(item.sha256)

    def test_without_a_title_the_clip_id_is_used(self):
        [item] = program_to_playlist(_program(_clip(title=None)))[1].items
        self.assertEqual(item.title, "7")

    def test_http_only_in_development(self):
        clip = _clip(download_url="http://host.docker.internal:54321/storage/v1/object/sign/x.mp4?token=t")
        with self.assertRaisesRegex(ProgramError, "download_url"):
            program_to_playlist(_program(clip))
        [item] = program_to_playlist(_program(clip), allow_http=True)[1].items
        self.assertTrue(item.s3_url.startswith("http://"))

    def test_rejects_urls_that_are_not_https(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "https://", "/relative.mp4", 42):
            with self.subTest(url=url), self.assertRaisesRegex(ProgramError, "stream_url"):
                program_to_playlist(_program(_clip(stream_url=url)), allow_http=True)

    def test_rejects_invalid_programs(self):
        cases = {
            "not an object": [],
            "program_hash": {"manifest": {"version": 2, "clips": []}},
            "manifest": {"program_hash": "h"},
            "Version de manifiesto": _program(version=1),
            "clips": {"program_hash": "h", "manifest": {"version": 2}},
            "ni download_url": _program(_clip(stream_url=None, download_url=None)),
            "repetido": _program(_clip(clip_id=7), _clip(clip_id="7")),
            "clip_id": _program(_clip(clip_id=True)),
            "duration_s": _program(_clip(duration_s=0)),
            r"clips\[0\] no es": _program("not-a-clip"),
        }
        for reason, program in cases.items():
            with self.subTest(reason=reason), self.assertRaises(ProgramError):
                program_to_playlist(program)

    def test_an_empty_program_is_an_empty_playlist(self):
        self.assertEqual(program_to_playlist(_program())[1].items, [])


class TestFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_json_of_a_missing_file_is_none(self):
        self.assertIsNone(read_json(self.dir / "nope.json"))

    def test_control_defaults_to_playing(self):
        path = self.dir / "control.json"
        self.assertEqual(read_control_playback(path), "playing")
        path.write_text("{not json")
        self.assertEqual(read_control_playback(path), "playing")
        path.write_text(json.dumps({"playback": "paused"}))
        self.assertEqual(read_control_playback(path), "paused")

    def test_status_document(self):
        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        rejected = {"program_hash": "h2", "reason": "x"}
        self.assertEqual(
            status_document("h1", rejected, "playing", now),
            {"program_hash": "h1", "error": rejected, "playback": "playing", "updated_at": "2026-10-23T12:00:00+00:00"},
        )

    def test_write_json_atomic_replaces_the_file_and_leaves_no_temporary(self):
        path = self.dir / "status.json"
        write_json_atomic(path, {"a": 1})
        write_json_atomic(path, {"a": 2})
        self.assertEqual(json.loads(path.read_text()), {"a": 2})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["status.json"])

    def test_a_failed_write_keeps_the_previous_file(self):
        path = self.dir / "status.json"
        write_json_atomic(path, {"a": 1})
        with self.assertRaises(TypeError):
            write_json_atomic(path, {"a": object()})
        self.assertEqual(json.loads(path.read_text()), {"a": 1})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["status.json"])


if __name__ == "__main__":
    unittest.main()
