import hashlib
import tempfile
import threading
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from molcajete_player.downloader import MediaDownloader
from molcajete_player.models import MediaItem, Playlist
from molcajete_player.player import GST_AVAILABLE, MolcajetePlayer

if GST_AVAILABLE:
    from molcajete_player.player import Gst


def _run_idle_immediately(func, *args):
    """Sustituto de GLib.idle_add que ejecuta el callback en el acto."""
    func(*args)
    return 1


def _make_player(playlist, **kwargs):
    with patch.object(MolcajetePlayer, "_build_pipeline"):
        player = MolcajetePlayer(playlist=playlist, sink_type=kwargs.pop("sink_type", "fakesink"), **kwargs)
    player.pipeline = MagicMock()
    player.pipeline.set_state.return_value = Gst.StateChangeReturn.SUCCESS
    player.pipeline.get_state.return_value = (None, None, None)
    return player


class _GstTestCase(unittest.TestCase):
    def setUp(self):
        if not GST_AVAILABLE:
            self.skipTest("GStreamer no se encuentra disponible en este entorno de pruebas.")


class TestMolcajetePlayerErrorHandling(_GstTestCase):
    def setUp(self):
        super().setUp()
        self.item1 = MediaItem(id="i1", title="Item 1", remote_url="https://example.com/1.m3u8")
        self.item2 = MediaItem(id="i2", title="Item 2", remote_url="https://example.com/2.m3u8")
        self.playlist = Playlist(name="Test Playlist", loop=True, items=[self.item1, self.item2])

    def test_handle_error_advances_and_stops_on_exhaustion(self):
        player = _make_player(self.playlist, downloader=MagicMock())
        player.loop = MagicMock()
        player.loop.is_running.return_value = True

        self.assertEqual(player._consecutive_errors, 0)
        self.assertFalse(player.has_fatal_error)

        with patch.object(player, "play_item_at_index", return_value=True) as mock_play:
            player._handle_error("Error de conexion en item 1")
            self.assertEqual(player._consecutive_errors, 1)
            self.assertFalse(player.has_fatal_error)
            mock_play.assert_called_once_with(1)

        with patch.object(player, "stop") as mock_stop:
            player._handle_error("Error de conexion en item 2")
            self.assertEqual(player._consecutive_errors, 2)
            self.assertTrue(player.has_fatal_error)
            mock_stop.assert_called_once()

    def test_handle_eos_resets_consecutive_errors(self):
        player = _make_player(self.playlist, downloader=MagicMock())
        player._consecutive_errors = 1

        with patch.object(player, "play_item_at_index", return_value=True):
            player._handle_eos()
            self.assertEqual(player._consecutive_errors, 0)

    def test_error_callback_is_invoked(self):
        error_callback = MagicMock()
        player = _make_player(self.playlist, downloader=MagicMock(), on_error=error_callback)

        with patch.object(player, "play_item_at_index", return_value=True):
            player._handle_error("Fallo de red")
            error_callback.assert_called_once_with("Fallo de red")

    def test_item_without_uri_is_skipped_in_looping_playlist(self):
        items = [
            MediaItem(id="a", title="A", remote_url="https://example.com/a.mp4"),
            MediaItem(id="b", title="B sin URI"),
            MediaItem(id="c", title="C", remote_url="https://example.com/c.mp4"),
        ]
        player = _make_player(Playlist(name="Bucle", loop=True, items=items), downloader=MagicMock())
        eos_callback = MagicMock()
        player.on_eos_callback = eos_callback

        with patch("molcajete_player.player.GLib.idle_add", side_effect=_run_idle_immediately):
            player._handle_eos()

        self.assertEqual(player.current_index, 2)
        player.pipeline.set_property.assert_called_with("uri", "https://example.com/c.mp4")
        self.assertFalse(player._stopped)
        self.assertFalse(player.has_fatal_error)
        eos_callback.assert_not_called()

    def test_end_of_non_looping_playlist_stops_and_calls_on_eos(self):
        playlist = Playlist(name="Sin bucle", loop=False, items=[self.item1, self.item2])
        eos_callback = MagicMock()
        player = _make_player(playlist, downloader=MagicMock(), on_eos=eos_callback)
        player.current_index = 1

        player._handle_eos()

        self.assertTrue(player._stopped)
        eos_callback.assert_called_once()

    def test_scheduled_error_is_ignored_after_stop(self):
        error_callback = MagicMock()
        player = _make_player(self.playlist, downloader=MagicMock(), on_error=error_callback)
        player.stop()

        player._handle_error("Error tardio")

        error_callback.assert_not_called()
        self.assertEqual(player._consecutive_errors, 0)

    def test_gstreamer_error_message_redacts_signed_urls(self):
        player = _make_player(self.playlist, downloader=MagicMock())
        message = MagicMock()
        message.type = Gst.MessageType.ERROR
        err = MagicMock()
        err.message = "No se pudo abrir https://stream.mux.com/x.m3u8?token=secreto"
        message.parse_error.return_value = (err, "uri=https://cdn.example.com/a.ts?sig=otro-secreto")

        with patch.object(player, "_handle_error") as mock_handle, self.assertLogs("molcajete_player.player", "ERROR") as logs:
            player._on_bus_message(None, message)

        handled_msg = mock_handle.call_args[0][0]
        self.assertNotIn("secreto", handled_msg)
        self.assertNotIn("secreto", "\n".join(logs.output))


class TestNavigationAndState(_GstTestCase):
    def setUp(self):
        super().setUp()
        self.items = [
            MediaItem(id="v1", title="Video 1", remote_url="https://example.com/1.mp4"),
            MediaItem(id="v2", title="Video 2", remote_url="https://example.com/2.mp4"),
        ]

    def test_previous_wraps_only_when_looping(self):
        player = _make_player(Playlist(name="Bucle", loop=True, items=self.items), downloader=MagicMock())
        self.assertTrue(player.previous())
        self.assertEqual(player.current_index, 1)

        player = _make_player(Playlist(name="Sin bucle", loop=False, items=self.items), downloader=MagicMock())
        self.assertFalse(player.previous())
        self.assertEqual(player.current_index, 0)

    def test_play_resumes_from_paused_without_reloading_uri(self):
        player = _make_player(Playlist(name="P", items=self.items), downloader=MagicMock())
        player.pipeline.get_state.return_value = (None, Gst.State.PAUSED, None)

        self.assertTrue(player.play())

        player.pipeline.set_state.assert_called_once_with(Gst.State.PLAYING)
        player.pipeline.set_property.assert_not_called()
        player.downloader.resume.assert_called_once()

    def test_play_logs_redacted_remote_url(self):
        items = [MediaItem(id="s", title="Firmado", remote_url="https://stream.mux.com/a.m3u8?token=secreto")]
        player = _make_player(Playlist(name="P", items=items), downloader=MagicMock())

        with self.assertLogs("molcajete_player.player", "INFO") as logs:
            player.play_item_at_index(0)

        self.assertNotIn("secreto", "\n".join(logs.output))
        player.pipeline.set_property.assert_called_with("uri", "https://stream.mux.com/a.m3u8?token=secreto")

    def test_buffering_pauses_and_resumes(self):
        player = _make_player(Playlist(name="P", items=self.items), downloader=MagicMock())
        player.play_item_at_index(0)
        player.pipeline.set_state.reset_mock()

        player._handle_buffering(40)
        player.pipeline.set_state.assert_called_once_with(Gst.State.PAUSED)

        player.pipeline.set_state.reset_mock()
        player._handle_buffering(70)
        player.pipeline.set_state.assert_not_called()

        player._handle_buffering(100)
        player.pipeline.set_state.assert_called_once_with(Gst.State.PLAYING)

    def test_buffering_does_not_override_manual_pause(self):
        player = _make_player(Playlist(name="P", items=self.items), downloader=MagicMock())
        player.play_item_at_index(0)
        player.pause()
        player.pipeline.set_state.reset_mock()

        player._handle_buffering(100)

        player.pipeline.set_state.assert_not_called()

    def test_buffering_is_ignored_for_live_streams(self):
        player = _make_player(Playlist(name="P", items=self.items), downloader=MagicMock())
        player.pipeline.set_state.return_value = Gst.StateChangeReturn.NO_PREROLL
        player.play_item_at_index(0)
        player.pipeline.set_state.reset_mock()

        player._handle_buffering(10)

        player.pipeline.set_state.assert_not_called()

    def test_stop_is_safe_when_window_destroy_reenters(self):
        player = _make_player(Playlist(name="P", items=self.items), downloader=MagicMock())
        window = MagicMock()
        window.destroy.side_effect = lambda: player.stop()
        player._window = window
        player._window_xid = 0x42

        player.stop()

        window.destroy.assert_called_once()
        self.assertIsNone(player._window)
        self.assertIsNone(player._window_xid)

    def test_run_resumes_downloader_and_prunes_cache(self):
        downloader = MagicMock()
        player = _make_player(Playlist(name="P", items=self.items), downloader=downloader)

        with patch.object(player, "_init_gtk_window", return_value=False), \
                patch("molcajete_player.player.GLib.MainLoop"), \
                patch.object(player, "play"):
            player.run()

        downloader.resume.assert_called()
        downloader.prune_cache.assert_called_once_with(player.playlist.items)

    def test_run_skips_pruning_when_disabled(self):
        downloader = MagicMock()
        player = _make_player(Playlist(name="P", items=self.items), downloader=downloader, prune_cache=False)

        with patch.object(player, "_init_gtk_window", return_value=False), \
                patch("molcajete_player.player.GLib.MainLoop"), \
                patch.object(player, "play"):
            player.run()

        downloader.prune_cache.assert_not_called()

    def test_player_download_callback_runs_on_main_loop(self):
        downloader_callback = MagicMock()
        player_callback = MagicMock()
        downloader = MediaDownloader(cache_dir=tempfile.gettempdir(), on_download_complete=downloader_callback)
        player = _make_player(
            Playlist(name="P", items=self.items),
            downloader=downloader,
            on_download_complete=player_callback,
        )
        item, path = self.items[0], Path("/tmp/x.mp4")

        with patch("molcajete_player.player.GLib.idle_add") as mock_idle:
            downloader.on_download_complete(item, path)

        downloader_callback.assert_called_once_with(item, path)
        player_callback.assert_not_called()
        mock_idle.assert_called_once_with(player._dispatch_download_complete, item, path)

        player._dispatch_download_complete(item, path)
        player_callback.assert_called_once_with(item, path)

    def test_overlay_sink_not_used_outside_x11(self):
        player = _make_player(Playlist(name="P", items=self.items), downloader=MagicMock(), sink_type="auto")
        created = []

        def fake_make(factory, name):
            created.append(factory)
            return None if factory == "gtksink" else MagicMock(name=factory)

        with patch("molcajete_player.player.GTK_AVAILABLE", True), \
                patch("molcajete_player.player._is_x11_display", return_value=False), \
                patch("molcajete_player.player.Gst.ElementFactory.make", side_effect=fake_make):
            player._create_video_sink()

        self.assertEqual(created, ["gtksink", "autovideosink"])
        self.assertFalse(player._use_x11_overlay)


class TestAudioAlwaysDisabled(_GstTestCase):
    def _flags(self, player):
        return int(player.pipeline.get_property("flags"))

    def test_audio_is_always_disabled(self):
        player = MolcajetePlayer(Playlist(name="P"), sink_type="fakesink", downloader=MagicMock())
        self.assertFalse(self._flags(player) & (1 << 1))
        audio_sink = player.pipeline.get_property("audio-sink")
        self.assertEqual(audio_sink.get_factory().get_name(), "fakesink")
        self.assertTrue(player.pipeline.get_property("mute"))

    def test_audio_cannot_be_enabled(self):
        with self.assertRaises(TypeError):
            MolcajetePlayer(Playlist(name="P"), sink_type="fakesink", downloader=MagicMock(), audio=True)


class TestGaplessPreBuffer(_GstTestCase):
    def setUp(self):
        super().setUp()
        self.items = [
            MediaItem(id="a", title="A", remote_url="https://example.com/a.mp4"),
            MediaItem(id="b", title="B sin URI"),
            MediaItem(id="c", title="C", remote_url="https://example.com/c.mp4"),
        ]

    def test_about_to_finish_preloads_next_playable_item(self):
        player = _make_player(Playlist(name="P", loop=True, items=self.items), downloader=MagicMock())
        playbin = MagicMock()

        player._on_about_to_finish(playbin)

        playbin.set_property.assert_called_once_with("uri", "https://example.com/c.mp4")
        self.assertEqual(list(player._pending), [(2, "https://example.com/c.mp4")])
        self.assertEqual(player.current_index, 0)

    def test_about_to_finish_does_nothing_at_end_of_non_looping_list(self):
        player = _make_player(Playlist(name="P", loop=False, items=self.items), downloader=MagicMock())
        player.current_index = 2
        playbin = MagicMock()

        player._on_about_to_finish(playbin)

        playbin.set_property.assert_not_called()
        self.assertEqual(len(player._pending), 0)

    def test_about_to_finish_chains_from_last_pending_item(self):
        items = [MediaItem(id=str(i), title=str(i), remote_url=f"https://e/{i}.mp4") for i in range(3)]
        player = _make_player(Playlist(name="P", loop=True, items=items), downloader=MagicMock())
        playbin = MagicMock()

        player._on_about_to_finish(playbin)
        player._on_about_to_finish(playbin)

        self.assertEqual([idx for idx, _ in player._pending], [1, 2])

    def test_stream_start_confirms_preloaded_item(self):
        on_item_start = MagicMock()
        downloader = MagicMock()
        player = _make_player(
            Playlist(name="P", loop=True, items=self.items),
            downloader=downloader,
            on_item_start=on_item_start,
        )
        player._consecutive_errors = 1
        player._on_about_to_finish(MagicMock())

        player._on_stream_start()

        self.assertEqual(player.current_index, 2)
        self.assertEqual(player._consecutive_errors, 0)
        on_item_start.assert_called_once_with(2, self.items[2], "https://example.com/c.mp4")
        self.assertEqual(len(player._pending), 0)

    def test_initial_stream_start_is_not_taken_as_preload(self):
        on_item_start = MagicMock()
        player = _make_player(Playlist(name="P", loop=True, items=self.items), downloader=MagicMock(), on_item_start=on_item_start)

        player.play_item_at_index(0)
        on_item_start.assert_called_once()
        player._on_stream_start()

        self.assertEqual(player.current_index, 0)
        on_item_start.assert_called_once()

    def test_buffering_of_preloaded_item_does_not_pause_current(self):
        player = _make_player(Playlist(name="P", loop=True, items=self.items), downloader=MagicMock())
        player.play_item_at_index(0)
        player._on_about_to_finish(MagicMock())
        player.pipeline.set_state.reset_mock()

        player._handle_buffering(20)

        player.pipeline.set_state.assert_not_called()

    def test_starting_an_item_prefetches_next_rendition(self):
        items = [
            MediaItem(id="a", title="A", remote_url="https://e/a.mp4"),
            MediaItem(id="b", title="B", remote_url="https://e/b.mp4", s3_url="https://bucket/b.mp4"),
        ]
        downloader = MagicMock()
        player = _make_player(Playlist(name="P", loop=True, items=items), downloader=downloader)

        player.play_item_at_index(0)

        downloader.ensure_downloading.assert_called_once_with(items[1])

    def test_prefetch_can_be_disabled(self):
        items = [
            MediaItem(id="a", title="A", remote_url="https://e/a.mp4"),
            MediaItem(id="b", title="B", remote_url="https://e/b.mp4", s3_url="https://bucket/b.mp4"),
        ]
        downloader = MagicMock()
        player = _make_player(Playlist(name="P", loop=True, items=items), downloader=downloader, prefetch_next=False)

        player.play_item_at_index(0)

        downloader.ensure_downloading.assert_not_called()


class TestShutdown(_GstTestCase):
    def setUp(self):
        super().setUp()
        self.items = [MediaItem(id="a", title="A", remote_url="https://e/a.mp4")]

    def test_stop_halts_pipeline_before_anything_else(self):
        calls = []
        downloader = MagicMock()
        downloader.stop.side_effect = lambda *a, **k: calls.append("downloader")
        player = _make_player(Playlist(name="P", items=self.items), downloader=downloader)
        player.pipeline.set_state.side_effect = lambda state: calls.append(("pipeline", state))

        player.stop()

        self.assertEqual(calls[0], ("pipeline", Gst.State.NULL))
        self.assertIn("downloader", calls)

    def test_run_installs_and_removes_signal_handlers(self):
        player = _make_player(Playlist(name="P", items=self.items), downloader=MagicMock())

        with patch.object(player, "_init_gtk_window", return_value=False), \
                patch("molcajete_player.player.GLib.MainLoop"), \
                patch.object(player, "play"), \
                patch("molcajete_player.player.GLib.unix_signal_add", side_effect=[11, 12, 13]) as mock_add, \
                patch("molcajete_player.player.GLib.source_remove") as mock_remove:
            player.run()

        import signal as signal_module
        registered = [call.args[1] for call in mock_add.call_args_list]
        self.assertEqual(registered, [int(signal_module.SIGINT), int(signal_module.SIGTERM), int(signal_module.SIGHUP)])
        self.assertEqual(sorted(call.args[0] for call in mock_remove.call_args_list), [11, 12, 13])

    def test_shutdown_signal_stops_player(self):
        player = _make_player(Playlist(name="P", items=self.items), downloader=MagicMock())
        with patch.object(player, "stop") as mock_stop:
            self.assertFalse(player._on_shutdown_signal("SIGTERM"))
        mock_stop.assert_called_once()


class TestStreamToDiskSwitch(_GstTestCase):
    def test_switches_from_mux_stream_to_local_disk_after_s3_download_on_eos(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = b"fake-mp4-binary-content"
            expected_sha = hashlib.sha256(payload).hexdigest()
            mux_stream_url = "https://stream.mux.com/test-asset.m3u8"
            s3_rendition_url = (
                "https://my-bucket.s3.amazonaws.com/rendition.mp4"
                "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=sig123"
            )

            item = MediaItem(
                id="clip-mux-1",
                title="Video Usuario",
                remote_url=mux_stream_url,
                s3_url=s3_rendition_url,
                sha256=expected_sha,
            )
            playlist = Playlist(name="Playlist S3 Switch", loop=True, items=[item])

            download_done = threading.Event()
            downloader = MediaDownloader(
                cache_dir=tmpdir,
                retry_interval=0.05,
                max_retries=5,
                on_download_complete=lambda it, path: download_done.set(),
            )
            expected_cache_file = item.resolve_cache_path(tmpdir)

            # Simulamos que S3 devuelve 404 en el primer intento (rendition aun generandose en Mux)
            # y en el segundo intento devuelve 200 OK con el contenido del MP4.
            attempts = {"count": 0}

            def fake_open(req):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise urllib.error.HTTPError(
                        url=s3_rendition_url,
                        code=404,
                        msg="Not Found",
                        hdrs=None,
                        fp=None,
                    )
                resp = MagicMock()
                resp.__enter__.return_value = BytesIO(payload)
                resp.__exit__.return_value = False
                return resp

            player = _make_player(playlist, downloader=downloader)

            with patch.object(downloader, "_open", side_effect=fake_open):
                # 1. Al iniciar reproduccion, no existe en disco -> reproduce el stream de Mux
                player.play()
                player.pipeline.set_property.assert_called_with("uri", mux_stream_url)

                # 2. Esperamos a que el descargador en segundo plano termine de bajar de S3
                finished = download_done.wait(timeout=2.0)
                self.assertTrue(finished, "La descarga desde S3 no termino a tiempo.")
                self.assertEqual(attempts["count"], 2)
                self.assertTrue(expected_cache_file.is_file())
                self.assertEqual(expected_cache_file.read_bytes(), payload)
                self.assertIsNone(item.local_path)
                self.assertEqual(item.cache_path, str(expected_cache_file))

                # 3. Al terminar la reproduccion actual (EOS), la siguiente vuelta usa el archivo en disco
                with patch.object(MediaItem, "verify_integrity") as mock_verify:
                    player._handle_eos()
                    # La verificacion hecha durante la descarga se reutiliza: no se vuelve a hashear.
                    mock_verify.assert_not_called()
                player.pipeline.set_property.assert_called_with(
                    "uri", expected_cache_file.resolve().as_uri()
                )

            player.stop()

    def test_corrupt_cache_file_is_discarded_before_playback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            item = MediaItem(
                id="clip-corrupt",
                title="Corrupto",
                remote_url="https://stream.mux.com/c.m3u8",
                s3_url="https://bucket.s3.amazonaws.com/c.mp4",
                sha256="a" * 64,
            )
            downloader = MediaDownloader(cache_dir=tmpdir)
            cache_file = item.resolve_cache_path(tmpdir)
            cache_file.write_bytes(b"basura")
            player = _make_player(Playlist(name="P", items=[item]), downloader=downloader)

            with patch.object(downloader, "ensure_downloading") as mock_ensure:
                player.play_item_at_index(0)

            self.assertFalse(cache_file.exists())
            player.pipeline.set_property.assert_called_with("uri", "https://stream.mux.com/c.m3u8")
            mock_ensure.assert_called_once_with(item)

    def test_persistent_window_handle_maintained_across_video_changes(self):
        item1 = MediaItem(id="v1", title="Video 1", remote_url="https://example.com/1.mp4")
        item2 = MediaItem(id="v2", title="Video 2", remote_url="https://example.com/2.mp4")
        playlist = Playlist(name="Kiosk Persistent Window", loop=True, items=[item1, item2])

        player = _make_player(playlist, sink_type="auto", downloader=MagicMock())
        player.video_sink = MagicMock()
        mock_window = MagicMock()
        player._window = mock_window
        player._window_xid = 0x123456

        with patch("molcajete_player.player._set_element_window_handle") as mock_set_handle:
            player.play_item_at_index(0)
            self.assertIs(player._window, mock_window)
            self.assertEqual(player._window_xid, 0x123456)
            mock_window.destroy.assert_not_called()

            player.next()
            self.assertIs(player._window, mock_window)
            self.assertEqual(player._window_xid, 0x123456)
            mock_window.destroy.assert_not_called()
            self.assertTrue(mock_set_handle.called)


if __name__ == "__main__":
    unittest.main()


class TestContractMode(_GstTestCase):
    """Lo que necesita el contrato con el agente: cambiar de lista en marcha, no cerrarse y decir que hace."""

    def setUp(self):
        super().setUp()
        self.item1 = MediaItem(id="i1", title="Item 1", remote_url="https://example.com/1.m3u8")
        self.item2 = MediaItem(id="i2", title="Item 2", remote_url="https://example.com/2.m3u8")

    def _player(self, items=(), keep_alive=True):
        return _make_player(Playlist(name="p", loop=True, items=list(items)), downloader=MagicMock(), keep_alive=keep_alive)

    def test_load_playlist_starts_the_new_list_from_the_first_item(self):
        player = self._player([self.item1])
        with patch.object(player, "play_item_at_index", return_value=True) as play:
            player.load_playlist(Playlist(name="nueva", loop=True, items=[self.item2, self.item1]))
        play.assert_called_once_with(0)
        self.assertEqual(player.playlist.name, "nueva")

    def test_load_playlist_while_paused_waits_for_resume(self):
        player = self._player([self.item1])
        player.pause()
        with patch.object(player, "play_item_at_index") as play:
            player.load_playlist(Playlist(name="nueva", loop=True, items=[self.item2]))
        play.assert_not_called()
        self.assertEqual(player.playback_state(), "paused")

    def test_an_empty_list_leaves_nothing_playing(self):
        player = self._player([self.item1])
        player.load_playlist(Playlist(name="vacia", loop=True, items=[]))
        player.pipeline.set_state.assert_called_with(Gst.State.NULL)
        self.assertIsNone(player.playback_state())

    def test_playback_state_reflects_what_is_happening(self):
        player = self._player()
        self.assertIsNone(player.playback_state())  # sin lista
        player.load_playlist(Playlist(name="p", loop=True, items=[self.item1]))
        self.assertEqual(player.playback_state(), "playing")
        player.pause()
        self.assertEqual(player.playback_state(), "paused")
        player.play()
        self.assertEqual(player.playback_state(), "playing")

    def test_pause_without_items_is_only_remembered(self):
        player = self._player()
        player.pause()
        player.pipeline.set_state.assert_not_called()

    def test_with_keep_alive_it_does_not_stop_when_every_item_fails_and_retries_later(self):
        player = self._player([self.item1, self.item2])
        player._consecutive_errors = 1
        with patch("molcajete_player.player.GLib") as glib, patch.object(player, "stop") as stop:
            glib.timeout_add_seconds.return_value = 99
            player._handle_error("fallo")
        stop.assert_not_called()
        self.assertTrue(player.has_fatal_error)
        self.assertIsNone(player.playback_state())
        glib.timeout_add_seconds.assert_called_once_with(MolcajetePlayer.FATAL_RETRY_SECONDS, player._retry_after_fatal)

        player._target_state = Gst.State.PLAYING
        with patch.object(player, "play_item_at_index", return_value=True) as play:
            self.assertFalse(player._retry_after_fatal())
        play.assert_called_once_with(0)
        self.assertFalse(player.has_fatal_error)

    def test_without_keep_alive_it_still_stops_when_every_item_fails(self):
        player = self._player([self.item1], keep_alive=False)
        with patch.object(player, "stop") as stop:
            player._handle_error("fallo")
        stop.assert_called_once()

    def test_a_new_list_cancels_the_pending_retry(self):
        player = self._player([self.item1])
        player._fatal_retry_source = 7
        with patch("molcajete_player.player.GLib") as glib, patch.object(player, "play_item_at_index", return_value=True):
            player.load_playlist(Playlist(name="nueva", loop=True, items=[self.item2]))
        glib.source_remove.assert_called_once_with(7)
        self.assertIsNone(player._fatal_retry_source)
