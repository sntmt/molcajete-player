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
            enable_download=False,
        )
        player.pipeline = MagicMock()

        with patch.object(player, "next", return_value=True):
            player._handle_error("Fallo de red")
            error_callback.assert_called_once_with("Fallo de red")

    @patch.object(MolcajetePlayer, "_build_pipeline")
    def test_downloader_starts_on_run_and_stops_on_stop(self, mock_build):
        mock_downloader = MagicMock()
        player = MolcajetePlayer(
            playlist=self.playlist,
            sink_type="fakesink",
            downloader=mock_downloader,
        )
        player.pipeline = MagicMock()

        with patch.object(player, "play"):
            with patch("gi.repository.GLib.MainLoop") as mock_loop_cls:
                mock_loop_instance = MagicMock()
                mock_loop_cls.return_value = mock_loop_instance
                player.run()
                mock_downloader.start_downloads_for_playlist.assert_called_once_with(self.playlist)
                mock_loop_instance.run.assert_called_once()

        player.stop()
        mock_downloader.stop.assert_called()

    @patch.object(MolcajetePlayer, "_build_pipeline")
    def test_play_item_transitions_from_remote_to_local(self, mock_build):
        player = MolcajetePlayer(
            playlist=self.playlist,
            sink_type="fakesink",
            enable_download=False,
        )
        player.pipeline = MagicMock()

        # Primera reproduccion: desde streaming remoto
        with patch.object(self.item1, "get_effective_uri", return_value="https://example.com/1.m3u8"):
            with self.assertLogs("molcajete_player.player", level="INFO") as cm:
                player.play_item_at_index(0)
                self.assertIn(self.item1.id, player._previously_remote_items)
                self.assertTrue(any("[REPRODUCIENDO STREAMING]" in msg for msg in cm.output))

        # Segunda reproduccion: tras completarse la descarga local
        with patch.object(self.item1, "get_effective_uri", return_value="file:///tmp/media/1.mp4"):
            with self.assertLogs("molcajete_player.player", level="INFO") as cm:
                player.play_item_at_index(0)
                self.assertNotIn(self.item1.id, player._previously_remote_items)
                self.assertTrue(any("[CONMUTACION A LOCAL]" in msg for msg in cm.output))
                self.assertTrue(any("[REPRODUCIENDO LOCAL]" in msg for msg in cm.output))


if __name__ == "__main__":
    unittest.main()
