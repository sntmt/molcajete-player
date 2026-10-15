import json
import tempfile
import unittest
from pathlib import Path

from molcajete_player.loader import load_playlist_from_json


class TestLoader(unittest.TestCase):
    def test_load_from_sample_file(self):
        sample_path = Path(__file__).resolve().parent.parent / "examples" / "sample_playlist.json"
        playlist = load_playlist_from_json(sample_path)
        self.assertEqual(playlist.name, "Lista de Reproduccion de Demostracion")
        self.assertTrue(playlist.loop)
        self.assertEqual(len(playlist.items), 3)
        self.assertEqual(playlist.items[0].id, "clip-001")

    def test_load_from_string(self):
        raw_json = json.dumps({
            "name": "Lista en Cadena",
            "loop": False,
            "items": [{"id": "v1", "title": "Clip 1", "remote_url": "http://example.com/v.mp4"}]
        })
        playlist = load_playlist_from_json(raw_json)
        self.assertEqual(playlist.name, "Lista en Cadena")
        self.assertFalse(playlist.loop)
        self.assertEqual(len(playlist.items), 1)

    def test_load_invalid_json(self):
        with self.assertRaises(ValueError):
            load_playlist_from_json("{ json invalido")


if __name__ == "__main__":
    unittest.main()
