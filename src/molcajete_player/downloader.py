from __future__ import annotations

import logging
import threading
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional

from molcajete_player.models import MediaItem, Playlist

logger = logging.getLogger(__name__)


class DownloadStatus(str, Enum):
    """Estados del proceso de descarga de un elemento multimedia."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AssetDownloader:
    """Gestor de descargas en segundo plano para almacenamiento en disco local."""

    def __init__(
        self,
        max_concurrent: int = 2,
        chunk_size: int = 64 * 1024,
        timeout: float = 30.0,
        on_item_downloaded: Optional[Callable[[MediaItem, Path], None]] = None,
        on_item_failed: Optional[Callable[[MediaItem, str], None]] = None,
    ):
        """Inicializa el gestor de descargas.

        Args:
            max_concurrent: Numero maximo de descargas simultaneas.
            chunk_size: Tamano de bloque en bytes para lectura y escritura.
            timeout: Tiempo de espera en segundos para solicitudes de red.
            on_item_downloaded: Callback invocado cuando un elemento finaliza su descarga.
            on_item_failed: Callback invocado si la descarga de un elemento falla.
        """
        self.max_concurrent = max(1, max_concurrent)
        self.chunk_size = chunk_size
        self.timeout = timeout
        self.on_item_downloaded = on_item_downloaded
        self.on_item_failed = on_item_failed

        self._executor: Optional[ThreadPoolExecutor] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._statuses: Dict[str, DownloadStatus] = {}
        self._futures: List[Future[bool]] = []

    def _ensure_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_concurrent,
                thread_name_prefix="asset-downloader",
            )
        return self._executor

    def get_status(self, item_id: str) -> DownloadStatus:
        """Devuelve el estado de descarga de un elemento."""
        with self._lock:
            return self._statuses.get(item_id, DownloadStatus.PENDING)

    def is_completed(self, item_id: str) -> bool:
        """Indica si el elemento ya esta descargado y disponible en disco."""
        return self.get_status(item_id) == DownloadStatus.COMPLETED

    def download_item(self, item: MediaItem) -> bool:
        """Ejecuta la descarga de un elemento de forma sincrona en el hilo actual."""
        if not item.local_path:
            logger.debug("Elemento '%s' no define 'local_path'. Descarga omitida.", item.id)
            with self._lock:
                self._statuses[item.id] = DownloadStatus.SKIPPED
            return False

        dest_path = Path(item.local_path).expanduser()
        if dest_path.is_file():
            logger.debug("Elemento '%s' ya existe en disco: %s", item.id, dest_path)
            with self._lock:
                self._statuses[item.id] = DownloadStatus.COMPLETED
            return True

        download_url = item.get_download_url()
        if not download_url:
            logger.warning(
                "Elemento '%s' no dispone de 'download_url' valida para descarga directa. Descarga omitida.",
                item.id,
            )
            with self._lock:
                self._statuses[item.id] = DownloadStatus.SKIPPED
            return False

        with self._lock:
            self._statuses[item.id] = DownloadStatus.DOWNLOADING

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = dest_path.with_name(f"{dest_path.name}.part")

        logger.info(
            "[INICIO DESCARGA] Iniciando descarga en segundo plano para '%s' (%s) -> %s",
            item.title,
            item.id,
            dest_path,
        )

        req = urllib.request.Request(
            download_url,
            headers={"User-Agent": "MolcajetePlayer/0.1.0"},
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                total_size = 0
                if hasattr(response, "headers") and response.headers is not None:
                    content_len = response.headers.get("Content-Length")
                    try:
                        if content_len is not None:
                            total_size = int(content_len)
                    except (ValueError, TypeError):
                        total_size = 0

                downloaded_bytes = 0
                last_logged_step = -1

                with open(part_path, "wb") as file_out:
                    while not self._stop_event.is_set():
                        chunk = response.read(self.chunk_size)
                        if not chunk:
                            break
                        file_out.write(chunk)
                        downloaded_bytes += len(chunk)

                        if total_size > 0:
                            percent = int((downloaded_bytes / total_size) * 100)
                            # Reportar cada 10% o al alcanzar el 100%
                            current_step = (percent // 10) * 10
                            if (current_step > last_logged_step or percent == 100) and percent > 0:
                                last_logged_step = current_step
                                logger.info(
                                    "[DESCARGANDO] Descargando '%s' (%s): %d%% (%d / %d bytes)",
                                    item.title,
                                    item.id,
                                    percent,
                                    downloaded_bytes,
                                    total_size,
                                )
                        else:
                            # Si no se informa Content-Length, reportar progreso cada 512 KB
                            step_kb = (downloaded_bytes // (512 * 1024)) * 512
                            if step_kb > last_logged_step:
                                last_logged_step = step_kb
                                logger.info(
                                    "[DESCARGANDO] Descargando '%s' (%s): %d KB transferidos",
                                    item.title,
                                    item.id,
                                    downloaded_bytes // 1024,
                                )

            if self._stop_event.is_set():
                logger.info("[DESCARGA CANCELADA] Descarga cancelada para '%s'. Eliminando archivo temporal.", item.id)
                if part_path.exists():
                    part_path.unlink()
                with self._lock:
                    self._statuses[item.id] = DownloadStatus.FAILED
                return False

            part_path.replace(dest_path)
            with self._lock:
                self._statuses[item.id] = DownloadStatus.COMPLETED

            logger.info(
                "[DESCARGA EXITOSA] Descarga de '%s' (%s) completada con exito en: %s",
                item.title,
                item.id,
                dest_path,
            )

            if self.on_item_downloaded:
                self.on_item_downloaded(item, dest_path)

            return True

        except Exception as exc:
            if part_path.exists():
                try:
                    part_path.unlink()
                except OSError:
                    pass

            with self._lock:
                self._statuses[item.id] = DownloadStatus.FAILED

            err_msg = f"[DESCARGA FALLIDA] Fallo al descargar elemento '{item.title}' ({item.id}): {exc}"
            logger.error(err_msg)

            if self.on_item_failed:
                self.on_item_failed(item, err_msg)

            return False

    def submit(self, item: MediaItem) -> Future[bool]:
        """Encola la descarga de un elemento multimedia en el grupo de hilos."""
        executor = self._ensure_executor()
        with self._lock:
            self._statuses[item.id] = DownloadStatus.PENDING

        future = executor.submit(self.download_item, item)
        self._futures.append(future)
        return future

    def start_downloads_for_playlist(self, playlist: Playlist) -> List[Future[bool]]:
        """Inicia la descarga en segundo plano de todos los elementos de la lista."""
        self._stop_event.clear()
        futures: List[Future[bool]] = []
        for item in playlist.items:
            dest_path = Path(item.local_path).expanduser() if item.local_path else None
            if dest_path and dest_path.is_file():
                with self._lock:
                    self._statuses[item.id] = DownloadStatus.COMPLETED
                continue

            if item.get_download_url():
                futures.append(self.submit(item))
            else:
                with self._lock:
                    self._statuses[item.id] = DownloadStatus.SKIPPED

        return futures

    def stop(self, wait: bool = False) -> None:
        """Detiene todas las descargas activas y libera el grupo de hilos."""
        self._stop_event.set()
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=True)
            self._executor = None
