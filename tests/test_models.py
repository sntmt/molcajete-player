import hashlib
import tempfile
import unittest
from pathlib import Path

from molcajete_player.models import (
    MediaItem,
    Playlist,
    canonicalize_url,
    extract_safe_extension,
    redact_url_for_log,
    redact_urls_in_text,
)


class TestMediaItem(unittest.TestCase):
    def test_from_dict_valid(self):
        valid_sha = "a" * 64
        data = {
            "id": "v1",
            "title": "Prueba",
            "remote_url": "https://example.com/video.m3u8",
            "local_path": "/tmp/video.mp4",
            "sha256": f"SHA256:{valid_sha.upper()}",
            "duration": 12.5,
            "metadata": {"fps": 30},
        }
        item = MediaItem.from_dict(data)
        self.assertEqual(item.id, "v1")
        self.assertEqual(item.title, "Prueba")
        self.assertEqual(item.sha256, valid_sha)
        self.assertEqual(item.duration, 12.5)
        self.assertEqual(item.metadata["fps"], 30)

    def test_from_dict_missing_fields(self):
        with self.assertRaises(ValueError):
            MediaItem.from_dict({"title": "Falta id"})

    def test_rejects_path_traversal_in_local_path(self):
        with self.assertRaises(ValueError):
            MediaItem(
                id="clip-evil",
                title="Path Traversal",
                local_path="../../etc/cron.d/pwn",
                remote_url="https://example.com/stream.m3u8",
            )

    def test_rejects_invalid_sha256(self):
        with self.assertRaises(ValueError):
            MediaItem(
                id="clip-bad-hash",
                title="Hash Invalido",
                remote_url="https://example.com/stream.m3u8",
                sha256="no-es-un-sha256-valido",
            )

    def test_deterministic_cache_filename_with_presigned_urls_and_sanitized_id(self):
        url_sig_1 = (
            "https://my-bucket.s3.amazonaws.com/renditions/intro.mp4"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=11111"
        )
        url_sig_2 = (
            "https://my-bucket.s3.amazonaws.com/renditions/intro.mp4"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=22222"
        )
        item1 = MediaItem(id="../kiosk/clip 01!", title="Clip 1", s3_url=url_sig_1)
        item2 = MediaItem(id="../kiosk/clip 01!", title="Clip 1", s3_url=url_sig_2)

        name1 = item1.build_cache_filename()
        name2 = item2.build_cache_filename()
        self.assertEqual(name1, name2)
        self.assertTrue(name1.startswith("kiosk_clip_01_"))
        self.assertTrue(name1.endswith(".mp4"))
        self.assertNotIn("..", name1)
        self.assertNotIn("/", name1)

        with tempfile.TemporaryDirectory() as tmpdir:
            resolved = item1.resolve_cache_path(tmpdir)
            self.assertEqual(resolved.parent, Path(tmpdir).resolve())

    def test_verify_integrity_with_sha256(self):
        payload = b"contenido-multimedia-verificado"
        expected_sha = hashlib.sha256(payload).hexdigest()

        with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp_file:
            Path(tmp_file.name).write_bytes(payload)
            item_ok = MediaItem(
                id="ok",
                title="Integro",
                local_path=tmp_file.name,
                sha256=expected_sha,
            )
            self.assertTrue(item_ok.verify_integrity())

            item_bad = MediaItem(
                id="bad",
                title="Corrupto",
                local_path=tmp_file.name,
                sha256="0" * 64,
            )
            self.assertFalse(item_bad.verify_integrity())

    def test_redact_url_for_log_hides_presigned_query(self):
        presigned = "https://bucket.s3.amazonaws.com/video.mp4?X-Amz-Signature=secret123"
        self.assertEqual(
            redact_url_for_log(presigned),
            "https://bucket.s3.amazonaws.com/video.mp4?<redacted>",
        )

    def test_get_effective_uri_prefers_local_when_exists(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp_file:
            item = MediaItem(
                id="local-first",
                title="Local First",
                local_path=tmp_file.name,
                remote_url="https://example.com/fallback.m3u8",
            )
            uri = item.get_effective_uri()
            self.assertTrue(uri.startswith("file://"))
            self.assertIn(Path(tmp_file.name).name, uri)

    def test_get_effective_uri_uses_remote_when_local_absent(self):
        item = MediaItem(
            id="remote-fallback",
            title="Remote Fallback",
            local_path="/ruta/inexistente/en/disco/video.mp4",
            remote_url="https://example.com/stream.m3u8",
        )
        uri = item.get_effective_uri()
        self.assertEqual(uri, "https://example.com/stream.m3u8")

    def test_get_effective_uri_raises_when_no_source(self):
        item = MediaItem(id="no-source", title="Sin Fuente")
        with self.assertRaises(ValueError):
            item.get_effective_uri()


class TestPlaylist(unittest.TestCase):
    def test_from_dict(self):
        data = {
            "name": "Lista Principal",
            "loop": True,
            "items": [
                {"id": "1", "title": "Intro", "remote_url": "https://example.com/1.mp4"},
                {"id": "2", "title": "Outro", "remote_url": "https://example.com/2.mp4"},
            ],
        }
        pl = Playlist.from_dict(data)
        self.assertEqual(pl.name, "Lista Principal")
        self.assertTrue(pl.loop)
        self.assertEqual(len(pl.items), 2)
        self.assertEqual(pl.items[0].id, "1")

    def test_from_dict_missing_name(self):
        with self.assertRaises(ValueError):
            Playlist.from_dict({"items": []})

    def test_loop_accepts_boolean_strings(self):
        self.assertFalse(Playlist.from_dict({"name": "n", "loop": "false"}).loop)
        self.assertTrue(Playlist.from_dict({"name": "n", "loop": "TRUE"}).loop)
        self.assertFalse(Playlist.from_dict({"name": "n"}).loop)

    def test_loop_rejects_non_boolean_values(self):
        for value in ("si", 1, None, "0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Playlist.from_dict({"name": "n", "loop": value})

    def test_rejects_non_object_items(self):
        with self.assertRaises(ValueError):
            Playlist.from_dict({"name": "n", "items": ["identity title"]})

    def test_rejects_duplicate_ids(self):
        with self.assertRaises(ValueError):
            Playlist.from_dict({
                "name": "n",
                "items": [
                    {"id": "x", "title": "A", "remote_url": "https://e/a.mp4"},
                    {"id": "x", "title": "B", "remote_url": "https://e/b.mp4"},
                ],
            })

    def test_confine_local_paths_applies_to_all_items(self):
        with tempfile.TemporaryDirectory() as root:
            playlist = Playlist(name="n", items=[MediaItem(id="a", title="A", local_path="/etc/passwd")])
            with self.assertRaises(ValueError):
                playlist.confine_local_paths(root)


class TestMediaItemValidation(unittest.TestCase):
    def test_rejects_null_title_and_id(self):
        with self.assertRaises(ValueError):
            MediaItem.from_dict({"id": "x", "title": None})
        with self.assertRaises(ValueError):
            MediaItem.from_dict({"id": None, "title": "T"})

    def test_rejects_non_object_metadata(self):
        with self.assertRaises(ValueError):
            MediaItem.from_dict({"id": "x", "title": "T", "metadata": ["no", "objeto"]})

    def test_null_metadata_becomes_empty_dict(self):
        self.assertEqual(MediaItem.from_dict({"id": "x", "title": "T", "metadata": None}).metadata, {})

    def test_has_immediate_source(self):
        self.assertTrue(MediaItem(id="a", title="A", remote_url="https://e/a.mp4").has_immediate_source())
        self.assertFalse(MediaItem(id="b", title="B", s3_url="https://e/b.mp4").has_immediate_source())


class TestLocalPathConfinement(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def test_relative_path_is_resolved_inside_root(self):
        item = MediaItem(id="a", title="A", local_path="videos/a.mp4")
        item.confine_local_path(self.root)
        self.assertEqual(item.local_path, str(self.root / "videos" / "a.mp4"))

    def test_absolute_path_outside_root_is_rejected(self):
        item = MediaItem(id="a", title="A", local_path="/etc/passwd")
        with self.assertRaises(ValueError):
            item.confine_local_path(self.root)

    def test_symlink_escaping_root_is_rejected(self):
        link = self.root / "escape.mp4"
        link.symlink_to("/etc/passwd")
        item = MediaItem(id="a", title="A", local_path="escape.mp4")
        with self.assertRaises(ValueError):
            item.confine_local_path(self.root)


class TestUrlHelpers(unittest.TestCase):
    def test_redact_urls_in_text(self):
        text = "fallo en https://a.example.com/x.m3u8?token=s1 y http://b.example.com/y.ts?sig=s2."
        redacted = redact_urls_in_text(text)
        self.assertNotIn("s1", redacted)
        self.assertNotIn("s2", redacted)
        self.assertIn("https://a.example.com/x.m3u8?<redacted>", redacted)

    def test_canonicalize_url_drops_query_and_lowercases_host(self):
        self.assertEqual(
            canonicalize_url("HTTPS://Bucket.S3.AmazonAWS.com/Path/v.mp4?X-Amz-Signature=1#frag"),
            "https://bucket.s3.amazonaws.com/Path/v.mp4",
        )

    def test_extract_safe_extension_rejects_unknown_extensions(self):
        self.assertEqual(extract_safe_extension("https://e/v.webm?sig=1"), ".webm")
        self.assertEqual(extract_safe_extension("https://e/payload.sh"), ".mp4")
        self.assertEqual(extract_safe_extension(None, default=".mkv"), ".mkv")


class TestEffectiveUriPriority(unittest.TestCase):
    def test_local_path_before_cache_path_before_remote(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as local, tempfile.NamedTemporaryFile(suffix=".mp4") as cached:
            item = MediaItem(id="p", title="P", local_path=local.name, remote_url="https://e/r.m3u8")
            item.cache_path = cached.name
            self.assertIn(Path(local.name).name, item.get_effective_uri())

            item.local_path = None
            self.assertIn(Path(cached.name).name, item.get_effective_uri())

            item.cache_path = "/no/existe.mp4"
            self.assertEqual(item.get_effective_uri(), "https://e/r.m3u8")

    def test_cache_path_is_not_serialized(self):
        item = MediaItem(id="p", title="P", remote_url="https://e/r.m3u8")
        item.cache_path = "/tmp/x.mp4"
        self.assertNotIn("cache_path", item.to_dict())


if __name__ == "__main__":
    unittest.main()
