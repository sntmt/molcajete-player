import unittest
from unittest.mock import MagicMock, patch

from molcajete_player.models import MediaItem, Playlist
from molcajete_player.player import MolcajetePlayer, GST_AVAILABLE


class TestMolcajetePlayerErrorHandling(unittest.TestCase):
    def setUp(self):
        if not GST_AVAILABLE:
            self.skipTest("GStreamer no se encuentra disponible en este entorno de pruebas.")

        self.item1 = MediaItem(id="i1", title="Item 1", remote_url="https://example.com/1.m3u8")
        self.item2 = MediaItem(id="i2", title="Item 2", remote_url="https://example.com/2.m3u8")
        self.playlist = Playlist(name="Test Playlist", loop=True, items=[self.item1, self.item2])

    @patch.object(MolcajetePlayer, "_build_pipeline")
    def test_handle_error_advances_and_stops_on_exhaustion(self, mock_build):
        player = MolcajetePlayer(playlist=self.playlist, sink_type="fakesink")
        player.pipeline = MagicMock()
        player.loop = MagicMock()
        player.loop.is_running.return_value = True

        self.assertEqual(player._consecutive_errors, 0)
        self.assertFalse(player.has_fatal_error)

        with patch.object(player, "next", return_value=True) as mock_next:
            player._handle_error("Error de conexion en item 1")
            self.assertEqual(player._consecutive_errors, 1)
            self.assertFalse(player.has_fatal_error)
            mock_next.assert_called_once()

        with patch.object(player, "stop") as mock_stop:
            player._handle_error("Error de conexion en item 2")
            self.assertEqual(player._consecutive_errors, 2)
            self.assertTrue(player.has_fatal_error)
            mock_stop.assert_called_once()

    @patch.object(MolcajetePlayer, "_build_pipeline")
    def test_handle_eos_resets_consecutive_errors(self, mock_build):
        player = MolcajetePlayer(playlist=self.playlist, sink_type="fakesink")
        player.pipeline = MagicMock()
        player._consecutive_errors = 1

        with patch.object(player, "next", return_value=True):
            player._handle_eos()
            self.assertEqual(player._consecutive_errors, 0)

    @patch.object(MolcajetePlayer, "_build_pipeline")
    def test_error_callback_is_invoked(self, mock_build):
        error_callback = MagicMock()
        player = MolcajetePlayer(
            playlist=self.playlist,
            sink_type="fakesink",
            on_error=error_callback,
        )
        player.pipeline = MagicMock()

        with patch.object(player, "next", return_value=True):
            player._handle_error("Fallo de red")
            error_callback.assert_called_once_with("Fallo de red")


if __name__ == "__main__":
    unittest.main()
