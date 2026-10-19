import json
import tempfile
import unittest
from pathlib import Path

from molcajete_player.loader import (
    load_playlist_from_file,
    load_playlist_from_json,
    load_playlist_from_string,
)


class TestLoader(unittest.TestCase):
    def test_load_from_sample_file(self):
        sample_path = Path(__file__).resolve().parent.parent / "examples" / "sample_playlist.json"
        playlist = load_playlist_from_json(sample_path)
        self.assertEqual(playlist.name, "Lista de Reproduccion de Demostracion")
        self.assertTrue(playlist.loop)
        self.assertEqual(len(playlist.items), 3)
        self.assertEqual(playlist.items[0].id, "flower-540p")

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

    def test_load_from_file_missing_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            load_playlist_from_file("/ruta/que/no/existe.json")

    def test_load_from_json_with_missing_path_object_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            load_playlist_from_json(Path("/ruta/que/no/existe.json"))

    def test_load_from_string_rejects_non_object_root(self):
        with self.assertRaises(ValueError):
            load_playlist_from_string("[1, 2, 3]")

    def test_load_from_file_confines_local_paths_to_media_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            playlist_file = root / "lista.json"
            playlist_file.write_text(json.dumps({
                "name": "Local",
                "items": [{"id": "a", "title": "A", "local_path": "clips/a.mp4"}],
            }))
            playlist = load_playlist_from_file(playlist_file, media_root=root)
            self.assertEqual(playlist.items[0].local_path, str(root / "clips" / "a.mp4"))

            playlist_file.write_text(json.dumps({
                "name": "Fuera",
                "items": [{"id": "a", "title": "A", "local_path": "/etc/passwd"}],
            }))
            with self.assertRaises(ValueError):
                load_playlist_from_file(playlist_file, media_root=root)


if __name__ == "__main__":
    unittest.main()
