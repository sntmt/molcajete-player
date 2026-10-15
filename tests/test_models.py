import tempfile
import unittest
from pathlib import Path

from molcajete_player.models import MediaItem, Playlist


class TestMediaItem(unittest.TestCase):
    def test_from_dict_valid(self):
        data = {
            "id": "v1",
            "title": "Prueba",
            "remote_url": "https://example.com/video.m3u8",
            "local_path": "/tmp/video.mp4",
            "duration": 12.5,
            "metadata": {"fps": 30},
        }
        item = MediaItem.from_dict(data)
        self.assertEqual(item.id, "v1")
        self.assertEqual(item.title, "Prueba")
        self.assertEqual(item.duration, 12.5)
        self.assertEqual(item.metadata["fps"], 30)

    def test_from_dict_missing_fields(self):
        with self.assertRaises(ValueError):
            MediaItem.from_dict({"title": "Falta id"})

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


if __name__ == "__main__":
    unittest.main()
