from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, Optional, Tuple, Union

from molcajete_player.models import MediaItem, redact_url_for_log

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "molcajete-player" / "media"
DEFAULT_MAX_BYTES = 8 * 1024**3
DEFAULT_MIN_FREE_BYTES = 256 * 1024**2

_RETRYABLE_HTTP_CODES = (403, 404, 500, 502, 503, 504)
# Nombres generados por MediaItem.build_cache_filename() y sus temporales ".<nombre>.part".
_CACHE_FILE_RE = re.compile(r"^[A-Za-z0-9_-]+_[0-9a-f]{16}\.(?:mp4|m4v|mov|webm|mkv|ts)$")
_PART_FILE_RE = re.compile(r"^\.[A-Za-z0-9_-]+_[0-9a-f]{16}\.(?:mp4|m4v|mov|webm|mkv|ts)\.part$")


def validate_download_url(
    url: str,
    allow_insecure_http: bool = False,
    allow_loopback_http: bool = True,
) -> None:
    """Valida que la URL de descarga utilice HTTPS (permitiendo loopback local para pruebas si se habilita)."""
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()

    if not hostname:
        raise ValueError(f"Invalid download URL (no host): {redact_url_for_log(url)}")

    if scheme == "https":
        return

    if scheme == "http":
        if allow_insecure_http:
            return
        if allow_loopback_http and hostname in ("127.0.0.1", "localhost", "::1"):
            return
        raise ValueError(
            f"Insecure download URL rejected (HTTPS required in kiosk mode): {redact_url_for_log(url)}"
        )

    raise ValueError(
        f"URL scheme '{scheme}' not allowed in download URL: {redact_url_for_log(url)}"
    )


class UnsafeRedirectError(urllib.error.URLError):
    """Redireccion HTTP rechazada por degradar el esquema o apuntar a una URL no permitida."""


class _DownloadAborted(Exception):
    """Fallo definitivo de una descarga que no debe reintentarse."""


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Aplica validate_download_url() a cada salto de redireccion e impide pasar de https:// a http://."""

    def __init__(self, allow_insecure_http: bool) -> None:
        super().__init__()
        self.allow_insecure_http = allow_insecure_http

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old_scheme = urllib.parse.urlsplit(req.full_url).scheme.lower()
        new_scheme = urllib.parse.urlsplit(newurl).scheme.lower()
        if old_scheme == "https" and new_scheme != "https" and not self.allow_insecure_http:
            raise UnsafeRedirectError(
                f"Redirect from https to {new_scheme} rejected: {redact_url_for_log(newurl)}"
            )
        try:
            validate_download_url(newurl, allow_insecure_http=self.allow_insecure_http)
        except ValueError as exc:
            raise UnsafeRedirectError(str(exc)) from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class MediaDownloader:
    """Gestor de descargas en segundo plano para renditions en S3 hacia un directorio de cache aislado.

    Cada elemento se descarga en su propio hilo daemon (``s3-downloader-<id>``).

    Args:
        cache_dir: Directorio de cache. Por defecto ``MOLCAJETE_CACHE_DIR`` o
            ``~/.cache/molcajete-player/media``.
        retry_interval: Segundos entre reintentos ante HTTP 403/404/5xx o fallos de red.
        max_retries: Numero maximo de intentos por descarga.
        chunk_size: Tamano en bytes de cada lectura de la respuesta HTTP.
        timeout: Timeout de socket en segundos para cada peticion.
        allow_insecure_http: Permite ``http://`` hacia hosts que no son loopback.
        on_download_complete: Callback ``(item, path)`` invocado tras una descarga
            verificada. Se ejecuta en el hilo de descarga, no en el bucle principal
            de GLib/Gtk.
        max_bytes: Tamano maximo aceptado por archivo; ``None`` desactiva el limite.
        min_free_bytes: Espacio libre que debe quedar en el disco de la cache tras la descarga.
    """

    def __init__(
        self,
        cache_dir: Optional[Union[str, Path]] = None,
        retry_interval: float = 3.0,
        max_retries: int = 60,
        chunk_size: int = 65536,
        timeout: float = 15.0,
        allow_insecure_http: bool = False,
        on_download_complete: Optional[Callable[[MediaItem, Path], None]] = None,
        max_bytes: Optional[int] = DEFAULT_MAX_BYTES,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    ):
        env_cache = os.environ.get("MOLCAJETE_CACHE_DIR")
        raw_cache_dir = cache_dir if cache_dir is not None else (env_cache or DEFAULT_CACHE_DIR)
        self.cache_dir: Path = Path(raw_cache_dir).expanduser().resolve()

        self.retry_interval = retry_interval
        self.max_retries = max_retries
        self.chunk_size = chunk_size
        self.timeout = timeout
        self.allow_insecure_http = allow_insecure_http
        self.on_download_complete = on_download_complete
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes

        self._lock = threading.Lock()
        self._active_threads: Dict[str, threading.Thread] = {}
        self._stop_event = threading.Event()
        # Firma (tamano, mtime_ns) de archivos ya verificados, para no recalcular SHA-256 en cada vuelta.
        self._verified: Dict[str, Tuple[int, int]] = {}
        self._opener = urllib.request.build_opener(_ValidatingRedirectHandler(allow_insecure_http))

    def resolve_target_path(self, item: MediaItem) -> Path:
        """Determina la ruta segura dentro de cache_dir para un MediaItem y la asigna a ``item.cache_path``."""
        target_path = item.resolve_cache_path(self.cache_dir)
        item.cache_path = str(target_path)
        return target_path

    def is_cached_and_valid(self, item: MediaItem, path: Path) -> bool:
        """Indica si ``path`` existe y supera la verificacion SHA-256, reutilizando verificaciones previas."""
        try:
            stat_result = path.stat()
        except OSError:
            return False
        signature = (stat_result.st_size, stat_result.st_mtime_ns)
        key = str(path)
        with self._lock:
            if self._verified.get(key) == signature:
                return True
        if not item.verify_integrity(path):
            return False
        with self._lock:
            self._verified[key] = signature
        return True

    def discard_cached(self, item: MediaItem) -> None:
        """Elimina el archivo de cache de ``item`` (si existe) y olvida su verificacion."""
        target_path = self.resolve_target_path(item)
        with self._lock:
            self._verified.pop(str(target_path), None)
        self._remove_file(target_path)

    def prepare(self, item: MediaItem) -> Optional[Path]:
        """Comprueba la cache de un elemento con ``s3_url`` antes de reproducirlo.

        Asigna ``item.cache_path`` y elimina el archivo en cache si no supera la
        verificacion SHA-256.

        Returns:
            La ruta en cache si existe integra; ``None`` si no aplica o hay que descargarla.
        """
        if not item.s3_url:
            return None
        try:
            target_path = self.resolve_target_path(item)
        except ValueError as exc:
            logger.error("Cache path rejected for '%s': %s", item.id, exc)
            return None

        if not target_path.is_file():
            return None
        if self.is_cached_and_valid(item, target_path):
            return target_path

        logger.warning(
            "Cached file for '%s' (%s) failed SHA-256 verification. Deleting it to download again...",
            item.id,
            target_path,
        )
        self.discard_cached(item)
        return None

    def ensure_downloading(self, item: MediaItem) -> bool:
        """Inicia la descarga en segundo plano si el item tiene s3_url y aun no esta verificado en cache_dir.

        No descarga si existe el archivo preinstalado de ``local_path``.

        Returns:
            True si se inicio o ya estaba en curso una descarga; False si no aplica o ya existe integro en disco.
        """
        if not item.s3_url:
            return False

        if item.local_path and Path(item.local_path).expanduser().is_file():
            return False

        try:
            validate_download_url(
                item.s3_url,
                allow_insecure_http=self.allow_insecure_http,
            )
        except ValueError as exc:
            logger.error("Download rejected for '%s': %s", item.id, exc)
            return False

        target_path = self.prepare(item)
        if target_path is not None:
            return False
        if item.cache_path is None:
            return False
        target_path = Path(item.cache_path)

        with self._lock:
            if self._stop_event.is_set():
                return False

            existing = self._active_threads.get(item.id)
            if existing and existing.is_alive():
                return True

            thread = threading.Thread(
                target=self._download_worker,
                args=(item, target_path),
                name=f"s3-downloader-{item.id}",
                daemon=True,
            )
            self._active_threads[item.id] = thread
            thread.start()
            return True

    def _open(self, req: urllib.request.Request):
        """Abre la peticion HTTP aplicando la validacion de redirecciones."""
        return self._opener.open(req, timeout=self.timeout)

    def _check_disk_budget(self, item: MediaItem, content_length: Optional[int]) -> None:
        """Rechaza la descarga si supera ``max_bytes`` o dejaria menos de ``min_free_bytes`` libres."""
        if content_length is not None and self.max_bytes is not None and content_length > self.max_bytes:
            raise _DownloadAborted(
                f"announced size ({content_length} bytes) exceeds the allowed maximum ({self.max_bytes} bytes)"
            )
        free_bytes = shutil.disk_usage(self.cache_dir).free
        needed = (content_length or 0) + self.min_free_bytes
        if free_bytes < needed:
            raise _DownloadAborted(
                f"not enough free space in {self.cache_dir} ({free_bytes} bytes free, {needed} required)"
            )

    def _download_worker(self, item: MediaItem, target_path: Path) -> None:
        """Descarga ``item.s3_url`` a ``.<nombre>.part`` con reintentos y lo activa tras verificar SHA-256."""
        assert item.s3_url is not None

        part_path = target_path.with_name(f".{target_path.name}.part")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        safe_log_url = redact_url_for_log(item.s3_url)

        attempt = 0
        while not self._stop_event.is_set() and attempt < self.max_retries:
            if target_path.is_file() and self.is_cached_and_valid(item, target_path):
                return

            attempt += 1
            try:
                logger.info(
                    "Downloading rendition for '%s' (attempt %d/%d): %s",
                    item.id,
                    attempt,
                    self.max_retries,
                    safe_log_url,
                )
                req = urllib.request.Request(
                    item.s3_url,
                    headers={"User-Agent": "MolcajetePlayer/1.0"},
                )
                hasher = hashlib.sha256()
                written = 0
                with self._open(req) as response:
                    headers = getattr(response, "headers", None)
                    raw_length = headers.get("Content-Length") if headers is not None else None
                    content_length = int(raw_length) if raw_length and str(raw_length).isdigit() else None
                    self._check_disk_budget(item, content_length)

                    with open(part_path, "wb") as out_file:
                        while not self._stop_event.is_set():
                            chunk = response.read(self.chunk_size)
                            if not chunk:
                                break
                            written += len(chunk)
                            if self.max_bytes is not None and written > self.max_bytes:
                                raise _DownloadAborted(
                                    f"download exceeded the allowed maximum ({self.max_bytes} bytes)"
                                )
                            hasher.update(chunk)
                            out_file.write(chunk)

                if self._stop_event.is_set():
                    self._remove_file(part_path)
                    return

                calculated_sha256 = hasher.hexdigest().lower()
                if item.sha256 and not hmac.compare_digest(calculated_sha256, item.sha256):
                    self._remove_file(part_path)
                    logger.error(
                        "SHA-256 integrity error downloading '%s': expected %s, got %s. "
                        "The downloaded file was discarded.",
                        item.id,
                        item.sha256,
                        calculated_sha256,
                    )
                    return

                part_path.replace(target_path)
                stat_result = target_path.stat()
                with self._lock:
                    self._verified[str(target_path)] = (stat_result.st_size, stat_result.st_mtime_ns)
                logger.info(
                    "Rendition download completed and verified for '%s' -> %s (sha256=%s). "
                    "The file on disk will be used the next time it plays.",
                    item.id,
                    target_path,
                    calculated_sha256[:16],
                )
                if self.on_download_complete:
                    self.on_download_complete(item, target_path)
                return

            except _DownloadAborted as exc:
                self._remove_file(part_path)
                logger.error("Download of '%s' cancelled: %s.", item.id, exc)
                return
            except UnsafeRedirectError as exc:
                self._remove_file(part_path)
                logger.error("Download of '%s' cancelled by an unsafe redirect: %s", item.id, exc.reason)
                return
            except urllib.error.HTTPError as exc:
                self._remove_file(part_path)
                if exc.code in _RETRYABLE_HTTP_CODES:
                    logger.info(
                        "Rendition for '%s' not available yet (HTTP %d). Retrying in %.1fs...",
                        item.id,
                        exc.code,
                        self.retry_interval,
                    )
                else:
                    logger.warning(
                        "HTTP error %d downloading rendition for '%s' (%s): %s",
                        exc.code,
                        item.id,
                        safe_log_url,
                        exc,
                    )
                    return
            except Exception as exc:
                self._remove_file(part_path)
                logger.warning(
                    "Temporary failure downloading rendition for '%s' (%s). Retrying in %.1fs...",
                    item.id,
                    exc,
                    self.retry_interval,
                )

            if self._stop_event.wait(self.retry_interval):
                return

        if attempt >= self.max_retries and not target_path.is_file():
            logger.error(
                "Gave up after %d attempts waiting for the rendition of '%s'.",
                self.max_retries,
                item.id,
            )

    @staticmethod
    def _remove_file(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    def _iter_cache_files(self) -> Iterator[Path]:
        """Recorre solo los archivos de cache_dir con nombres generados por el reproductor."""
        if not self.cache_dir.is_dir():
            return
        for entry in self.cache_dir.iterdir():
            if entry.is_file() and (_CACHE_FILE_RE.match(entry.name) or _PART_FILE_RE.match(entry.name)):
                yield entry

    def prune_cache(self, items: Iterable[MediaItem]) -> int:
        """Elimina de cache_dir los renditions que no pertenecen a ``items`` y los ``.part`` huerfanos.

        Solo toca archivos cuyo nombre sigue el patron de cache del reproductor;
        cualquier otro archivo del directorio se conserva.

        Returns:
            Numero de archivos eliminados.
        """
        keep = set()
        for item in items:
            if item.s3_url:
                try:
                    keep.add(item.resolve_cache_path(self.cache_dir).name)
                except ValueError:
                    continue
        with self._lock:
            downloading = any(thread.is_alive() for thread in self._active_threads.values())

        removed = 0
        for entry in self._iter_cache_files():
            if entry.name in keep:
                continue
            if entry.name.endswith(".part") and downloading:
                continue
            self._remove_file(entry)
            removed += 1
        if removed:
            logger.info("Removed %d stale files from cache %s.", removed, self.cache_dir)
        return removed

    def clear_cache(self) -> int:
        """Elimina todos los renditions y temporales del reproductor en cache_dir (conserva el directorio).

        Returns:
            Numero de archivos eliminados.
        """
        removed = 0
        for entry in self._iter_cache_files():
            self._remove_file(entry)
            removed += 1
        with self._lock:
            self._verified.clear()
        return removed

    def resume(self) -> None:
        """Permite iniciar descargas de nuevo tras un ``stop()``."""
        self._stop_event.clear()

    def stop(self, join_timeout: float = 0.0) -> None:
        """Detiene todas las descargas activas en segundo plano.

        Tras llamarla, ``ensure_downloading`` no inicia descargas nuevas hasta que se
        invoque ``resume()``.

        Args:
            join_timeout: Segundos maximos a esperar por cada hilo activo (0 = no esperar).
        """
        self._stop_event.set()
        if join_timeout <= 0:
            return
        with self._lock:
            threads = list(self._active_threads.values())
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(join_timeout)
