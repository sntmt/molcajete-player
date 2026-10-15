import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

from molcajete_player.downloader import AssetDownloader, DownloadStatus
from molcajete_player.models import MediaItem, Playlist


class TestAssetDownloader(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dest_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_media_item_get_download_url(self):
        item1 = MediaItem(
            id="1",
            title="Con download_url",
            download_url="https://example.com/video.mp4",
            remote_url="https://example.com/stream.m3u8",
        )
        self.assertEqual(item1.get_download_url(), "https://example.com/video.mp4")

        item2 = MediaItem(
            id="2",
            title="Con remote mp4",
            remote_url="https://example.com/direct.mp4",
        )
        self.assertEqual(item2.get_download_url(), "https://example.com/direct.mp4")

        item3 = MediaItem(
            id="3",
            title="Solo HLS stream",
            remote_url="https://example.com/stream.m3u8",
        )
        self.assertIsNone(item3.get_download_url())

    @patch("urllib.request.urlopen")
    def test_download_success(self, mock_urlopen):
        mock_response = io.BytesIO(b"fake video data chunks")
        mock_urlopen.return_value.__enter__.return_value.read.side_effect = [
            b"fake video ",
            b"data chunks",
            b"",
        ]

        dest_file = self.dest_dir / "media" / "video.mp4"
        item = MediaItem(
            id="vid-1",
            title="Test Video",
            download_url="https://example.com/file.mp4",
            local_path=str(dest_file),
        )

        on_downloaded = MagicMock()
        downloader = AssetDownloader(on_item_downloaded=on_downloaded)

        success = downloader.download_item(item)

        self.assertTrue(success)
        self.assertTrue(dest_file.is_file())
        self.assertEqual(dest_file.read_bytes(), b"fake video data chunks")
        self.assertEqual(downloader.get_status("vid-1"), DownloadStatus.COMPLETED)
        on_downloaded.assert_called_once_with(item, dest_file)

    def test_download_skips_when_file_already_exists(self):
        existing_file = self.dest_dir / "already_here.mp4"
        existing_file.write_bytes(b"existing content")

        item = MediaItem(
            id="vid-exist",
            title="Existing Video",
            download_url="https://example.com/file.mp4",
            local_path=str(existing_file),
        )

        downloader = AssetDownloader()
        with patch("urllib.request.urlopen") as mock_urlopen:
            success = downloader.download_item(item)
            mock_urlopen.assert_not_called()

        self.assertTrue(success)
        self.assertEqual(downloader.get_status("vid-exist"), DownloadStatus.COMPLETED)

    def test_download_skips_when_no_download_url_or_local_path(self):
        downloader = AssetDownloader()

        # Sin local_path
        item1 = MediaItem(id="no-path", title="No Path", remote_url="https://example.com/file.mp4")
        self.assertFalse(downloader.download_item(item1))
        self.assertEqual(downloader.get_status("no-path"), DownloadStatus.SKIPPED)

        # Sin download_url valida (solo m3u8)
        item2 = MediaItem(
            id="no-url",
            title="No URL",
            remote_url="https://example.com/stream.m3u8",
            local_path="/tmp/video.mp4",
        )
        self.assertFalse(downloader.download_item(item2))
        self.assertEqual(downloader.get_status("no-url"), DownloadStatus.SKIPPED)

    @patch("urllib.request.urlopen")
    def test_download_failure_cleans_up_part_file(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        dest_file = self.dest_dir / "failed.mp4"
        part_file = self.dest_dir / "failed.mp4.part"
        item = MediaItem(
            id="vid-fail",
            title="Failed Video",
            download_url="https://example.com/file.mp4",
            local_path=str(dest_file),
        )

        on_failed = MagicMock()
        downloader = AssetDownloader(on_item_failed=on_failed)

        success = downloader.download_item(item)

        self.assertFalse(success)
        self.assertFalse(dest_file.exists())
        self.assertFalse(part_file.exists())
        self.assertEqual(downloader.get_status("vid-fail"), DownloadStatus.FAILED)
        on_failed.assert_called_once()

    def test_start_downloads_for_playlist(self):
        dest1 = self.dest_dir / "p1.mp4"
        dest2 = self.dest_dir / "p2.mp4"
        dest1.write_bytes(b"cached")

        item1 = MediaItem(id="1", title="Item 1", local_path=str(dest1), download_url="https://example.com/1.mp4")
        item2 = MediaItem(id="2", title="Item 2", local_path=str(dest2), download_url="https://example.com/2.mp4")
        item3 = MediaItem(id="3", title="Item 3", remote_url="https://example.com/stream.m3u8")

        playlist = Playlist(name="Test Playlist", items=[item1, item2, item3])

        downloader = AssetDownloader()
        with patch.object(downloader, "submit") as mock_submit:
            downloader.start_downloads_for_playlist(playlist)
            mock_submit.assert_called_once_with(item2)

        self.assertEqual(downloader.get_status("1"), DownloadStatus.COMPLETED)
        self.assertEqual(downloader.get_status("3"), DownloadStatus.SKIPPED)
        downloader.stop()


if __name__ == "__main__":
    unittest.main()
