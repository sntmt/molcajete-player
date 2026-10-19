import hashlib
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from molcajete_player.downloader import (
    MediaDownloader,
    UnsafeRedirectError,
    _ValidatingRedirectHandler,
)
from molcajete_player.models import MediaItem


def _response(payload: bytes, content_length=None):
    body = BytesIO(payload)
    body.headers = {} if content_length is None else {"Content-Length": str(content_length)}
    resp = MagicMock()
    resp.__enter__.return_value = body
    resp.__exit__.return_value = False
    return resp


def _http_error(code: int):
    return urllib.error.HTTPError(url="https://x", code=code, msg="err", hdrs=None, fp=None)


class _DownloaderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)
        self.payload = b"rendition-mp4"
        self.item = MediaItem(
            id="clip",
            title="Clip",
            remote_url="https://stream.mux.com/clip.m3u8",
            s3_url="https://bucket.s3.amazonaws.com/clip.mp4?X-Amz-Signature=abc",
            sha256=hashlib.sha256(self.payload).hexdigest(),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def make_downloader(self, **kwargs):
        kwargs.setdefault("retry_interval", 0)
        kwargs.setdefault("max_retries", 3)
        return MediaDownloader(cache_dir=self.cache_dir, **kwargs)


class TestRedirectValidation(unittest.TestCase):
    def setUp(self):
        self.handler = _ValidatingRedirectHandler(allow_insecure_http=False)
        self.req = urllib.request.Request("https://bucket.s3.amazonaws.com/a.mp4")

    def test_rejects_https_to_http_downgrade(self):
        with self.assertRaises(UnsafeRedirectError):
            self.handler.redirect_request(self.req, None, 302, "Found", {}, "http://evil.example.com/a.mp4")

    def test_rejects_https_to_loopback_http(self):
        with self.assertRaises(UnsafeRedirectError):
            self.handler.redirect_request(self.req, None, 302, "Found", {}, "http://127.0.0.1/a.mp4")

    def test_rejects_non_http_scheme(self):
        with self.assertRaises(UnsafeRedirectError):
            self.handler.redirect_request(self.req, None, 302, "Found", {}, "ftp://files.example.com/a.mp4")

    def test_allows_https_to_https(self):
        new_req = self.handler.redirect_request(self.req, None, 302, "Found", {}, "https://cdn.example.com/a.mp4")
        self.assertEqual(new_req.full_url, "https://cdn.example.com/a.mp4")

    def test_unsafe_redirect_aborts_without_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            item = MediaItem(id="r", title="R", s3_url="https://bucket.s3.amazonaws.com/r.mp4")
            downloader = MediaDownloader(cache_dir=tmpdir, retry_interval=0, max_retries=5)
            calls = {"n": 0}

            def fake_open(req):
                calls["n"] += 1
                raise UnsafeRedirectError("degradacion")

            with patch.object(downloader, "_open", side_effect=fake_open):
                downloader._download_worker(item, downloader.resolve_target_path(item))
            self.assertEqual(calls["n"], 1)


class TestDownloadWorker(_DownloaderTestCase):
    def test_non_retryable_http_error_stops_immediately(self):
        downloader = self.make_downloader(max_retries=5)
        mock_open = MagicMock(side_effect=_http_error(401))
        with patch.object(downloader, "_open", mock_open):
            downloader._download_worker(self.item, downloader.resolve_target_path(self.item))
        self.assertEqual(mock_open.call_count, 1)

    def test_retries_until_max_retries_exhausted(self):
        downloader = self.make_downloader(max_retries=3)
        mock_open = MagicMock(side_effect=_http_error(404))
        with patch.object(downloader, "_open", mock_open), self.assertLogs("molcajete_player.downloader", "ERROR"):
            downloader._download_worker(self.item, downloader.resolve_target_path(self.item))
        self.assertEqual(mock_open.call_count, 3)

    def test_stop_during_download_discards_partial_file(self):
        downloader = self.make_downloader()
        target = downloader.resolve_target_path(self.item)

        class StoppingBody(BytesIO):
            headers = {}

            def read(inner_self, size=-1):
                downloader.stop()
                return b"parcial"

        resp = MagicMock()
        resp.__enter__.return_value = StoppingBody()
        resp.__exit__.return_value = False
        with patch.object(downloader, "_open", return_value=resp):
            downloader._download_worker(self.item, target)

        self.assertFalse(target.exists())
        self.assertEqual(list(self.cache_dir.iterdir()), [])

    def test_rejects_download_larger_than_max_bytes_by_header(self):
        downloader = self.make_downloader(max_bytes=4)
        mock_open = MagicMock(return_value=_response(self.payload, content_length=len(self.payload)))
        with patch.object(downloader, "_open", mock_open):
            downloader._download_worker(self.item, downloader.resolve_target_path(self.item))
        self.assertEqual(mock_open.call_count, 1)
        self.assertEqual(list(self.cache_dir.iterdir()), [])

    def test_rejects_download_larger_than_max_bytes_while_streaming(self):
        downloader = self.make_downloader(max_bytes=4, chunk_size=2)
        mock_open = MagicMock(return_value=_response(self.payload))
        with patch.object(downloader, "_open", mock_open):
            downloader._download_worker(self.item, downloader.resolve_target_path(self.item))
        self.assertEqual(mock_open.call_count, 1)
        self.assertEqual(list(self.cache_dir.iterdir()), [])

    def test_rejects_download_without_enough_free_space(self):
        downloader = self.make_downloader(min_free_bytes=1000)
        usage = MagicMock(free=500)
        with patch.object(downloader, "_open", return_value=_response(self.payload, len(self.payload))), \
                patch("molcajete_player.downloader.shutil.disk_usage", return_value=usage):
            downloader._download_worker(self.item, downloader.resolve_target_path(self.item))
        self.assertEqual(list(self.cache_dir.iterdir()), [])

    def test_successful_download_is_verified_and_remembered(self):
        downloader = self.make_downloader()
        target = downloader.resolve_target_path(self.item)
        with patch.object(downloader, "_open", return_value=_response(self.payload, len(self.payload))):
            downloader._download_worker(self.item, target)

        self.assertEqual(target.read_bytes(), self.payload)
        with patch.object(MediaItem, "verify_integrity") as mock_verify:
            self.assertTrue(downloader.is_cached_and_valid(self.item, target))
            mock_verify.assert_not_called()


class TestDownloadSecurity(_DownloaderTestCase):
    def test_downloader_discards_file_on_sha256_mismatch(self):
        item = MediaItem(
            id="clip-tampered",
            title="Video Corrupto",
            remote_url="https://stream.mux.com/test.m3u8",
            s3_url="https://my-bucket.s3.amazonaws.com/tampered.mp4",
            sha256="f" * 64,
        )
        downloader = self.make_downloader(max_retries=2)
        target_path = downloader.resolve_target_path(item)

        with patch.object(downloader, "_open", return_value=_response(b"datos-alterados-por-atacante")):
            downloader._download_worker(item, target_path)

        self.assertFalse(target_path.exists())

    def test_downloader_rejects_remote_insecure_http_url(self):
        item = MediaItem(
            id="clip-http",
            title="HTTP Inseguro",
            remote_url="https://stream.mux.com/test.m3u8",
            s3_url="http://my-bucket.s3.amazonaws.com/insecure.mp4",
        )
        downloader = self.make_downloader(allow_insecure_http=False)
        self.assertFalse(downloader.ensure_downloading(item))


class TestEnsureDownloading(_DownloaderTestCase):
    def test_deduplicates_threads_for_same_item(self):
        downloader = self.make_downloader()
        release = threading.Event()
        calls = {"n": 0}

        def blocking_open(req):
            calls["n"] += 1
            release.wait(2)
            return _response(self.payload, len(self.payload))

        with patch.object(downloader, "_open", side_effect=blocking_open):
            self.assertTrue(downloader.ensure_downloading(self.item))
            self.assertTrue(downloader.ensure_downloading(self.item))
            release.set()
            downloader.stop(join_timeout=2)
        self.assertEqual(calls["n"], 1)

    def test_resume_allows_downloads_after_stop(self):
        downloader = self.make_downloader()
        downloader.stop()
        self.assertFalse(downloader.ensure_downloading(self.item))

        downloader.resume()
        with patch.object(downloader, "_open", return_value=_response(self.payload, len(self.payload))):
            self.assertTrue(downloader.ensure_downloading(self.item))
            downloader.stop(join_timeout=2)

    def test_preinstalled_local_path_skips_download_and_is_not_overwritten(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as preinstalled:
            item = MediaItem(
                id="pre",
                title="Preinstalado",
                local_path=preinstalled.name,
                s3_url="https://bucket.s3.amazonaws.com/pre.mp4",
            )
            downloader = self.make_downloader()
            self.assertFalse(downloader.ensure_downloading(item))
            downloader.resolve_target_path(item)
            self.assertEqual(item.local_path, preinstalled.name)
            self.assertTrue(item.get_effective_uri().endswith(Path(preinstalled.name).name))


class TestCacheVerificationAndMaintenance(_DownloaderTestCase):
    def test_is_cached_and_valid_rehashes_only_when_file_changes(self):
        downloader = self.make_downloader()
        target = downloader.resolve_target_path(self.item)
        target.write_bytes(self.payload)

        with patch.object(MediaItem, "verify_integrity", autospec=True, side_effect=lambda it, p: True) as mock_verify:
            self.assertTrue(downloader.is_cached_and_valid(self.item, target))
            self.assertTrue(downloader.is_cached_and_valid(self.item, target))
            self.assertEqual(mock_verify.call_count, 1)

            stat = target.stat()
            os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            self.assertTrue(downloader.is_cached_and_valid(self.item, target))
            self.assertEqual(mock_verify.call_count, 2)

    def test_prepare_discards_corrupt_cache(self):
        downloader = self.make_downloader()
        target = downloader.resolve_target_path(self.item)
        target.write_bytes(b"manipulado")

        self.assertIsNone(downloader.prepare(self.item))
        self.assertFalse(target.exists())

    def test_prepare_returns_valid_cache(self):
        downloader = self.make_downloader()
        target = downloader.resolve_target_path(self.item)
        target.write_bytes(self.payload)

        self.assertEqual(downloader.prepare(self.item), target)

    def test_prune_cache_removes_only_stale_player_files(self):
        downloader = self.make_downloader()
        current = downloader.resolve_target_path(self.item)
        current.write_bytes(self.payload)
        stale = self.cache_dir / "old-clip_0123456789abcdef.mp4"
        stale.write_bytes(b"viejo")
        orphan_part = self.cache_dir / ".old-clip_0123456789abcdef.mp4.part"
        orphan_part.write_bytes(b"parcial")
        unrelated = self.cache_dir / "notas.txt"
        unrelated.write_text("no tocar")

        removed = downloader.prune_cache([self.item])

        self.assertEqual(removed, 2)
        self.assertTrue(current.exists())
        self.assertTrue(unrelated.exists())
        self.assertFalse(stale.exists())
        self.assertFalse(orphan_part.exists())

    def test_clear_cache_keeps_unrelated_files(self):
        downloader = self.make_downloader()
        downloader.resolve_target_path(self.item).write_bytes(self.payload)
        unrelated = self.cache_dir / "video_personal.mp4"
        unrelated.write_bytes(b"mio")

        self.assertEqual(downloader.clear_cache(), 1)
        self.assertTrue(unrelated.exists())
        self.assertTrue(self.cache_dir.is_dir())

    def test_maintenance_on_missing_cache_dir_is_noop(self):
        downloader = MediaDownloader(cache_dir=self.cache_dir / "no-existe")
        self.assertEqual(downloader.prune_cache([self.item]), 0)
        self.assertEqual(downloader.clear_cache(), 0)


if __name__ == "__main__":
    unittest.main()
