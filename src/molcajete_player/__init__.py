"""Molcajete Player

Reproductor de contenido audiovisual orquestado con Python y GStreamer.
"""

from molcajete_player.downloader import AssetDownloader, DownloadStatus
from molcajete_player.loader import load_playlist_from_json
from molcajete_player.models import MediaItem, Playlist
from molcajete_player.player import MolcajetePlayer

__version__ = "0.1.0"
__all__ = [
    "AssetDownloader",
    "DownloadStatus",
    "MediaItem",
    "MolcajetePlayer",
    "Playlist",
    "load_playlist_from_json",
]
