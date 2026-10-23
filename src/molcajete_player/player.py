from __future__ import annotations

import logging
import signal
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional, Union

from molcajete_player.downloader import MediaDownloader
from molcajete_player.models import MediaItem, Playlist, redact_url_for_log, redact_urls_in_text

logger = logging.getLogger(__name__)

# Intentar importar GStreamer mediante PyGObject
try:
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib, Gst
    GST_AVAILABLE = True
except (ImportError, ValueError) as err:
    GST_AVAILABLE = False
    Gst = None
    GLib = None
    _GST_IMPORT_ERROR = str(err)

# GstVideo (gir1.2-gst-plugins-base-1.0) expone GstVideoOverlay de forma segura.
try:
    import gi
    gi.require_version("GstVideo", "1.0")
    from gi.repository import GstVideo
except (ImportError, ValueError):
    GstVideo = None

# Intentar importar Gtk y Gdk para entornos de escritorio con ventana nativa persistente
try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk
    GTK_AVAILABLE = True
except (ImportError, ValueError):
    GTK_AVAILABLE = False
    Gtk = None
    Gdk = None

# Intentar importar GdkX11 para incrustar video (GstVideoOverlay) en ventana Gtk
# incluso cuando el plugin gtksink (gstreamer1.0-gtk3) no esta instalado en el sistema.
try:
    import gi
    gi.require_version("GdkX11", "3.0")
    from gi.repository import GdkX11
    GDK_X11_AVAILABLE = True
except (ImportError, ValueError):
    GDK_X11_AVAILABLE = False
    GdkX11 = None

# Respaldo via ctypes cuando no esta instalado el typelib de GstVideo.
_libgstvideo: Optional[Any] = None
if GstVideo is None:
    try:
        import ctypes
        ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
        ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
        _libgstvideo = ctypes.CDLL("libgstvideo-1.0.so.0")
        _libgstvideo.gst_video_overlay_set_window_handle.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        _libgstvideo.gst_video_overlay_set_window_handle.restype = None
    except Exception:
        _libgstvideo = None

VIDEO_OVERLAY_AVAILABLE = GstVideo is not None or _libgstvideo is not None

# GstPlayFlags de playbin: bit de la pista de audio.
_PLAY_FLAG_AUDIO = 1 << 1
# Senales que provocan un cierre ordenado (mismo camino que cerrar la ventana).
_SHUTDOWN_SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP")


def _set_element_window_handle(element: Any, window_handle: int) -> bool:
    """Asigna el identificador de ventana nativa (XID) a un elemento GStreamer que implemente GstVideoOverlay."""
    if element is None or not window_handle:
        return False
    if GstVideo is not None:
        if not isinstance(element, GstVideo.VideoOverlay):
            return False
        element.set_window_handle(int(window_handle))
        return True
    if _libgstvideo is None:
        return False
    try:
        gpointer = getattr(element, "__gpointer__", None)
        if gpointer is None:
            return False
        ptr = ctypes.pythonapi.PyCapsule_GetPointer(gpointer, None)
        if not ptr:
            return False
        _libgstvideo.gst_video_overlay_set_window_handle(ptr, int(window_handle))
        return True
    except Exception:
        return False


def _is_x11_display() -> bool:
    """Indica si Gdk esta conectado a un servidor X11 (y no a Wayland u otro backend)."""
    if not (GDK_X11_AVAILABLE and Gdk is not None):
        return False
    display = Gdk.Display.get_default()
    return display is not None and isinstance(display, GdkX11.X11Display)


class MolcajetePlayer:
    """Controlador de reproduccion multimedia basado en GStreamer para Linux y entornos locales."""

    def __init__(
        self,
        playlist: Playlist,
        sink_type: str = "auto",
        on_eos: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        downloader: Optional[MediaDownloader] = None,
        cache_dir: Optional[Union[str, Path]] = None,
        on_download_complete: Optional[Callable[[MediaItem, Path], None]] = None,
        prune_cache: bool = True,
        prefetch_next: bool = True,
        on_item_start: Optional[Callable[[int, MediaItem, str], None]] = None,
        keep_alive: bool = False,
    ):
        """Inicializa el reproductor con una lista y el tipo de salida de video configurado.

        Args:
            playlist: Instancia de Playlist con los elementos multimedia a reproducir.
            sink_type: Tipo de salida de video ('auto', 'gtk', 'kms', 'fbdev', 'fakesink').
                       'auto': seleccion automatica (Gtk si esta disponible, autovideosink si no).
                       'gtk': renderizado integrado mediante ventana nativa Gtk (macOS / Linux X11/Wayland).
                       'kms': kmssink para salida directa DRM/KMS sin servidor grafico.
                       'fbdev': fbdevsink para framebuffer directo en Linux.
                       'fakesink': sin salida visual, util para entornos headless o pruebas.
            on_eos: Callback opcional al finalizar la lista de reproduccion.
            on_error: Callback opcional al producirse un error en el bus.
            downloader: Gestor de descargas en segundo plano para renditions en S3.
            cache_dir: Directorio de cache controlado donde almacenar renditions descargados.
            on_download_complete: Callback ``(item, path)`` tras una descarga verificada.
                A diferencia del callback de ``MediaDownloader``, se ejecuta en el bucle
                principal de GLib/Gtk, por lo que puede interactuar con GStreamer y Gtk.
            prune_cache: Si es True, ``run()`` elimina de la cache los renditions que no
                pertenecen a la lista y los ``.part`` huerfanos antes de empezar.
            prefetch_next: Si es True, al empezar cada elemento se inicia en segundo plano
                la descarga del ``s3_url`` del siguiente.
            on_item_start: Callback ``(index, item, uri)`` invocado en el bucle principal
                cada vez que un elemento empieza a mostrarse, tanto por cambio sin huecos
                como por ``play_item_at_index``.
            keep_alive: Si es True (modo contrato con el agente), el reproductor no se cierra
                cuando fallan todos los elementos o se acaba la lista: espera y lo vuelve a
                intentar, y admite listas nuevas con ``load_playlist``. Si se cerrara, el agente
                lo relanzaria en bucle.
        """
        if not GST_AVAILABLE:
            raise RuntimeError(
                f"GStreamer is not available in this environment: {_GST_IMPORT_ERROR}. "
                "Check the PyGObject installation and the system libraries."
            )

        if not Gst.is_initialized():
            Gst.init(None)

        self.playlist = playlist
        self.sink_type = sink_type.lower()
        self.on_eos_callback = on_eos
        self.on_error_callback = on_error
        self.on_download_complete_callback = on_download_complete
        self.prune_cache = prune_cache
        self.prefetch_next = prefetch_next
        self.on_item_start_callback = on_item_start
        self.keep_alive = keep_alive
        self._fatal_retry_source: Optional[int] = None
        self.downloader = (
            downloader if downloader is not None else MediaDownloader(cache_dir=cache_dir)
        )
        if on_download_complete is not None:
            self._chain_download_callback()

        self.current_index = 0
        self.pipeline: Optional[Gst.Element] = None
        self.video_sink: Optional[Gst.Element] = None
        self.loop: Optional[GLib.MainLoop] = None
        self.loop_thread: Optional[threading.Thread] = None
        self._is_running = False
        self._stopped = False
        self._consecutive_errors = 0
        self.has_fatal_error = False
        self._use_gtk = False
        self._use_x11_overlay = False
        self._window: Optional[Any] = None
        self._drawing_area: Optional[Any] = None
        self._window_xid: Optional[int] = None
        self._target_state = Gst.State.NULL
        self._is_live = False
        self._buffering = False
        # Elementos ya cargados en playbin por about-to-finish, en orden, pendientes de su STREAM_START.
        self._pending_lock = threading.Lock()
        self._pending: deque = deque()
        # El STREAM_START del elemento iniciado con play_item_at_index no corresponde a ninguna precarga.
        self._awaiting_initial_stream_start = False
        self._signal_sources: list = []

        for item in self.playlist.items:
            if not item.has_immediate_source():
                logger.warning(
                    "Item '%s' only has s3_url: it cannot play until its download finishes.",
                    item.id,
                )

        self._build_pipeline()

    def _chain_download_callback(self) -> None:
        """Encadena el callback del descargador con un reenvio al bucle principal de GLib."""
        previous = self.downloader.on_download_complete

        def _on_download_complete(item: MediaItem, path: Path) -> None:
            if previous is not None:
                previous(item, path)
            GLib.idle_add(self._dispatch_download_complete, item, path)

        self.downloader.on_download_complete = _on_download_complete

    def _dispatch_download_complete(self, item: MediaItem, path: Path) -> bool:
        if self.on_download_complete_callback is not None:
            self.on_download_complete_callback(item, path)
        return False

    def _create_video_sink(self) -> Optional[Gst.Element]:
        """Crea el elemento de salida de video segun la configuracion de entorno."""
        if self.sink_type == "fakesink":
            sink = Gst.ElementFactory.make("fakesink", "video-sink")
            if sink is not None:
                sink.set_property("sync", True)
            return sink
        if self.sink_type == "kms":
            return Gst.ElementFactory.make("kmssink", "video-sink")
        if self.sink_type == "fbdev":
            return Gst.ElementFactory.make("fbdevsink", "video-sink")

        # Para entornos graficos ('auto' o 'gtk'):
        # 1. Si gtksink esta disponible, incrusta su widget directamente en la ventana Gtk.
        if GTK_AVAILABLE and self.sink_type in ("auto", "gtk"):
            gtksink = Gst.ElementFactory.make("gtksink", "video-sink")
            if gtksink is not None:
                self._use_gtk = True
                return gtksink

            # 2. Si gtksink no esta instalado pero disponemos de Gtk sobre X11 + GstVideoOverlay,
            # creamos una ventana Gtk persistente y embebemos xvimagesink/glimagesink/ximagesink
            # mediante su XID para que la ventana nunca se cierre ni recree al cambiar de video.
            # En Wayland no hay XID, asi que se usa autovideosink.
            if VIDEO_OVERLAY_AVAILABLE and _is_x11_display():
                for sink_name in ("xvimagesink", "glimagesink", "ximagesink"):
                    overlay_sink = Gst.ElementFactory.make(sink_name, "video-sink")
                    if overlay_sink is not None:
                        if overlay_sink.find_property("force-aspect-ratio") is not None:
                            overlay_sink.set_property("force-aspect-ratio", True)
                        self._use_gtk = True
                        self._use_x11_overlay = True
                        return overlay_sink

        return Gst.ElementFactory.make("autovideosink", "video-sink")

    def _build_pipeline(self) -> None:
        """Configura la tuberia playbin y conecta los manejadores del bus de mensajes."""
        self.pipeline = Gst.ElementFactory.make("playbin", "molcajete-playbin")
        if not self.pipeline:
            raise RuntimeError("Could not create the GStreamer 'playbin' element.")

        # El reproductor nunca emite sonido.
        self._disable_audio()

        # Pre-buffer: unos segundos antes del final, playbin pide la siguiente URI y la
        # prepara mientras el video actual sigue en pantalla (cambio sin huecos).
        self.pipeline.connect("about-to-finish", self._on_about_to_finish)

        self.video_sink = self._create_video_sink()
        if self.video_sink is not None:
            self.pipeline.set_property("video-sink", self.video_sink)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        if self._use_x11_overlay:
            bus.enable_sync_message_emission()
            bus.connect("sync-message::element", self._on_sync_message)

    def _disable_audio(self) -> None:
        """Garantiza que ningun video abra un flujo de sonido.

        Se aplican tres capas porque algunos elementos (p. ej. demuxers HLS) pueden
        exponer el audio aunque se quite el flag: sin el flag de audio no se decodifica;
        el ``audio-sink`` es un ``fakesink`` que nunca abre un dispositivo, y ``mute``
        silencia cualquier resto.
        """
        flags = int(self.pipeline.get_property("flags"))
        self.pipeline.set_property("flags", flags & ~_PLAY_FLAG_AUDIO)
        audio_sink = Gst.ElementFactory.make("fakesink", "audio-sink")
        if audio_sink is not None:
            audio_sink.set_property("sync", True)
            self.pipeline.set_property("audio-sink", audio_sink)
        if self.pipeline.find_property("mute") is not None:
            self.pipeline.set_property("mute", True)

    def _on_sync_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        """Intercepta prepare-window-handle de forma sincrona para asociar el XID de la ventana Gtk persistente."""
        if not self._window_xid:
            return
        struct = message.get_structure()
        if struct is not None and struct.get_name() == "prepare-window-handle":
            _set_element_window_handle(message.src, self._window_xid)

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        """Procesa mensajes emitidos por el bus de GStreamer."""
        t = message.type
        if t == Gst.MessageType.EOS:
            logger.info("End of stream reached (EOS).")
            self._handle_eos()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            current_item = (
                self.playlist.items[self.current_index]
                if 0 <= self.current_index < len(self.playlist.items)
                else None
            )
            item_desc = f"'{current_item.title}' ({current_item.id})" if current_item else "unknown"
            msg = redact_urls_in_text(f"Error in item {item_desc}: {err.message}. Details: {debug}")
            logger.error(msg)
            self._handle_error(msg)
        elif t == Gst.MessageType.BUFFERING:
            self._handle_buffering(message.parse_buffering())
        elif t == Gst.MessageType.STREAM_START:
            self._on_stream_start()
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                old_state, new_state, _ = message.parse_state_changed()
                logger.debug("Pipeline state change: %s -> %s", old_state.value_name, new_state.value_name)
                if new_state == Gst.State.PLAYING:
                    self._consecutive_errors = 0

    def _handle_buffering(self, percent: int) -> None:
        """Pausa mientras se llena el bufer de red y reanuda al 100 %, respetando una pausa manual.

        Los streams en directo (``NO_PREROLL``) no se pausan por buffering.
        """
        if self._is_live:
            return
        logger.debug("Buffering network stream: %d%%", percent)
        with self._pending_lock:
            transition_pending = bool(self._pending)
        if transition_pending and percent < 100:
            # El bufer pertenece al siguiente elemento precargado: pausar congelaria el actual.
            return
        if percent < 100:
            if not self._buffering:
                self._buffering = True
                if self._target_state == Gst.State.PLAYING:
                    self.pipeline.set_state(Gst.State.PAUSED)
        else:
            self._buffering = False
            if self._target_state == Gst.State.PLAYING:
                self.pipeline.set_state(Gst.State.PLAYING)

    def _next_index(self, from_index: Optional[int] = None) -> Optional[int]:
        """Indice del elemento posterior a ``from_index`` (por defecto el actual), o None al final de una lista sin bucle."""
        if not self.playlist.items:
            return None
        base = self.current_index if from_index is None else from_index
        next_idx = base + 1
        if next_idx >= len(self.playlist.items):
            if not self.playlist.loop:
                return None
            next_idx = 0
        return next_idx

    def _on_about_to_finish(self, playbin: Any) -> None:
        """Precarga la URI del siguiente elemento reproducible (se ejecuta en un hilo de GStreamer).

        Si ningun elemento posterior tiene URI, no se precarga nada: llegara un EOS y
        ``_handle_eos`` aplicara la logica normal de fin de lista o de errores.
        """
        if self._stopped:
            return
        with self._pending_lock:
            base = self._pending[-1][0] if self._pending else self.current_index
        idx = self._next_index(base)
        for _ in range(len(self.playlist.items)):
            if idx is None:
                return
            item = self.playlist.items[idx]
            if self.downloader:
                self.downloader.prepare(item)
            try:
                uri = item.get_effective_uri()
            except ValueError as exc:
                logger.warning("Skipping '%s' in preload: %s", item.id, exc)
                idx = self._next_index(idx)
                continue
            with self._pending_lock:
                self._pending.append((idx, uri))
            playbin.set_property("uri", uri)
            logger.info("Preloading next item '%s' -> %s", item.id, redact_url_for_log(uri))
            return

    def _on_stream_start(self) -> None:
        """Confirma en el bucle principal el cambio sin huecos al elemento precargado."""
        with self._pending_lock:
            if self._awaiting_initial_stream_start:
                self._awaiting_initial_stream_start = False
                return
            if not self._pending:
                return
            index, uri = self._pending.popleft()
        self.current_index = index
        self._consecutive_errors = 0
        self._announce_item(index, uri)

    def _announce_item(self, index: int, uri: str) -> None:
        """Registra el elemento que empieza a mostrarse e inicia sus descargas en segundo plano."""
        item = self.playlist.items[index]
        logger.info(
            "Playing item [%d/%d] '%s' (%s) -> URI: %s",
            index + 1,
            len(self.playlist.items),
            item.title,
            item.id,
            redact_url_for_log(uri),
        )

        if self.downloader and item.s3_url and not item.is_downloaded():
            self.downloader.ensure_downloading(item)
        if self.prefetch_next:
            self._prefetch_next_item()

        if self._window is not None and hasattr(self._window, "set_title"):
            self._window.set_title(f"Molcajete Player - {item.title}")

        if self.on_item_start_callback is not None:
            self.on_item_start_callback(index, item, uri)

    def _prefetch_next_item(self) -> None:
        """Inicia la descarga del siguiente elemento para que este en disco cuando le toque."""
        next_idx = self._next_index()
        if next_idx is None or next_idx == self.current_index or not self.downloader:
            return
        item = self.playlist.items[next_idx]
        if item.s3_url and not item.is_downloaded():
            self.downloader.ensure_downloading(item)

    def _handle_eos(self) -> None:
        """Avanza al siguiente elemento o finaliza la reproduccion tras completar una pista con exito."""
        self._consecutive_errors = 0
        next_idx = self._next_index()
        if next_idx is None:
            logger.info("Playlist finished.")
            if self.keep_alive:
                self._idle_pipeline()
            else:
                self.stop()
            if self.on_eos_callback:
                self.on_eos_callback()
            return
        # Si el siguiente elemento no puede iniciarse, play_item_at_index programa _handle_error,
        # que lo salta y continua con el resto de la lista.
        self.play_item_at_index(next_idx)

    def _handle_error(self, error_message: str) -> None:
        """Gestiona un fallo en la reproduccion evitando bucles infinitos."""
        if self._stopped:
            return
        self._consecutive_errors += 1
        if self.on_error_callback:
            self.on_error_callback(error_message)

        total_items = len(self.playlist.items)
        if total_items == 0 or self._consecutive_errors >= total_items:
            logger.critical(
                "All playlist items (%d) failed in a row. "
                "Stopping playback to avoid an endless error loop.",
                total_items,
            )
            self.has_fatal_error = True
            if self.keep_alive:
                self._idle_until_retry()
            else:
                self.stop()
            return

        next_idx = self._next_index()
        if next_idx is None:
            logger.info("End of the playlist reached after a playback failure.")
            if self.keep_alive:
                self._idle_pipeline()
            else:
                self.stop()
            return

        logger.warning(
            "Current item failed (%d/%d consecutive failures). Moving to the next item...",
            self._consecutive_errors,
            total_items,
        )
        self.play_item_at_index(next_idx)

    def _schedule_error(self, error_message: str) -> None:
        """Registra un fallo y lo procesa desde el bucle principal para no reentrar en la llamada actual."""
        logger.error(error_message)
        GLib.idle_add(self._handle_error, error_message)

    # Tras fallar todos los elementos en modo keep_alive, se vuelve a intentar pasado este tiempo.
    FATAL_RETRY_SECONDS = 30

    def _idle_pipeline(self) -> None:
        """Deja la tuberia parada sin salir del bucle de eventos (modo keep_alive)."""
        with self._pending_lock:
            self._pending.clear()
            self._awaiting_initial_stream_start = False
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)

    def _idle_until_retry(self) -> None:
        self._idle_pipeline()
        self._cancel_fatal_retry()
        logger.warning("Retrying the playlist in %d s.", self.FATAL_RETRY_SECONDS)
        self._fatal_retry_source = GLib.timeout_add_seconds(self.FATAL_RETRY_SECONDS, self._retry_after_fatal)

    def _cancel_fatal_retry(self) -> None:
        if self._fatal_retry_source is not None:
            GLib.source_remove(self._fatal_retry_source)
            self._fatal_retry_source = None

    def _retry_after_fatal(self) -> bool:
        self._fatal_retry_source = None
        self._consecutive_errors = 0
        self.has_fatal_error = False
        if self._target_state != Gst.State.PAUSED and self.playlist.items:
            self.play_item_at_index(0)
        return False

    def load_playlist(self, playlist: Playlist) -> None:
        """Cambia la lista en marcha, sin salir ni reiniciar el proceso.

        Empieza por el primer elemento; si se habia pedido pausa, queda en pausa y empezara al
        reanudar. Lo ya descargado en la cache se conserva. De momento el cambio corta el
        elemento en curso (cambiar sin cortarlo es otra tarea).
        """
        self._cancel_fatal_retry()
        paused = self._target_state == Gst.State.PAUSED
        self.playlist = playlist
        self.current_index = 0
        self._consecutive_errors = 0
        self.has_fatal_error = False
        if not playlist.items:
            logger.info("Empty playlist: nothing to play.")
            self._idle_pipeline()
            return
        if paused:
            self._idle_pipeline()
            return
        self.play_item_at_index(0)

    def playback_state(self) -> Optional[str]:
        """Lo que hace de verdad: ``playing``, ``paused`` o ``None`` (nada que reproducir o todo fallando)."""
        if self._stopped or self.has_fatal_error or not self.playlist.items:
            return None
        if self._target_state == Gst.State.PAUSED:
            return "paused"
        if self._target_state == Gst.State.PLAYING:
            return "playing"
        return None

    def play_item_at_index(self, index: int) -> bool:
        """Establece y reproduce el elemento correspondiente al indice indicado.

        Returns:
            True si la tuberia empezo a reproducir el elemento. False si el indice no es
            valido o el elemento no pudo iniciarse; en este ultimo caso el fallo ya se
            programo en ``_handle_error``, que avanzara al siguiente elemento.
        """
        if not self.playlist.items:
            logger.warning("The playlist has no items.")
            return False

        if index < 0 or index >= len(self.playlist.items):
            return False

        self._stopped = False
        with self._pending_lock:
            self._pending.clear()
            self._awaiting_initial_stream_start = True
        self.current_index = index
        item: MediaItem = self.playlist.items[self.current_index]

        if self.downloader:
            self.downloader.prepare(item)

        try:
            uri = item.get_effective_uri()
        except ValueError as exc:
            self._schedule_error(f"Could not get the URI for item '{item.id}': {exc}")
            return False

        self._announce_item(index, uri)

        self.pipeline.set_state(Gst.State.READY)
        if self._window_xid:
            _set_element_window_handle(self.pipeline, self._window_xid)
            _set_element_window_handle(self.video_sink, self._window_xid)
        self.pipeline.set_property("uri", uri)
        self._target_state = Gst.State.PLAYING
        self._buffering = False
        state_ret = self.pipeline.set_state(Gst.State.PLAYING)
        self._is_live = state_ret == Gst.StateChangeReturn.NO_PREROLL
        if state_ret == Gst.StateChangeReturn.FAILURE:
            self._schedule_error(
                f"Failed to set the pipeline to PLAYING for item '{item.id}'."
            )
            return False

        return True

    def play(self) -> bool:
        """Inicia o reanuda la reproduccion (reactivando el descargador si se habia detenido)."""
        if self.pipeline is None:
            return False

        self.downloader.resume()

        _, current_state, _ = self.pipeline.get_state(0)
        if current_state == Gst.State.PAUSED:
            self._target_state = Gst.State.PLAYING
            self.pipeline.set_state(Gst.State.PLAYING)
            return True

        return self.play_item_at_index(self.current_index)

    def pause(self) -> None:
        """Pausa la reproduccion actual (sin elementos, solo se recuerda para cuando lleguen)."""
        if self.pipeline:
            self._target_state = Gst.State.PAUSED
            if self.playlist.items and not self.has_fatal_error:
                self.pipeline.set_state(Gst.State.PAUSED)

    def stop(self) -> None:
        """Detiene la reproduccion y libera recursos de la tuberia.

        Tambien detiene el descargador (``play()``/``run()`` lo reactivan), sale del
        bucle de eventos y destruye la ventana Gtk si existe. Es seguro llamarla
        varias veces, incluida la reentrada desde la senal ``destroy`` de la ventana.
        """
        self._stopped = True
        self._target_state = Gst.State.NULL
        self._cancel_fatal_retry()
        with self._pending_lock:
            self._pending.clear()
            self._awaiting_initial_stream_start = False

        # Lo primero es parar la tuberia: si algo posterior falla, el audio y el video ya estan detenidos.
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)

        if self.downloader:
            self.downloader.stop()

        if self.loop and self.loop.is_running():
            self.loop.quit()

        if self._use_gtk and GTK_AVAILABLE and Gtk and Gtk.main_level() > 0:
            Gtk.main_quit()

        window = self._window
        self._window = None
        self._drawing_area = None
        self._window_xid = None
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

        self._is_running = False

    def next(self) -> bool:
        """Avanza al siguiente elemento de la lista.

        Returns:
            True si el siguiente elemento empezo a reproducirse; False si se llego al
            final de una lista sin bucle o si el elemento no pudo iniciarse.
        """
        next_idx = self._next_index()
        if next_idx is None:
            return False
        return self.play_item_at_index(next_idx)

    def previous(self) -> bool:
        """Retrocede al elemento anterior de la lista."""
        prev_idx = self.current_index - 1
        if prev_idx < 0:
            if self.playlist.loop:
                prev_idx = len(self.playlist.items) - 1
            else:
                return False

        return self.play_item_at_index(prev_idx)

    def _fallback_to_autovideosink(self) -> None:
        """Abandona la ventana Gtk persistente y deja que GStreamer elija su propia salida."""
        logger.warning("Could not get the Gtk window XID; using autovideosink.")
        window = self._window
        self._window = None
        self._drawing_area = None
        if window is not None:
            window.destroy()
        self._use_gtk = False
        self._use_x11_overlay = False
        sink = Gst.ElementFactory.make("autovideosink", "video-sink-fallback")
        if sink is not None:
            self.video_sink = sink
            self.pipeline.set_property("video-sink", sink)

    def _init_gtk_window(self) -> bool:
        """Crea y muestra la ventana Gtk persistente para kiosko/escritorio."""
        if not (self._use_gtk and GTK_AVAILABLE and Gtk):
            return False

        Gtk.init([])
        self._window = Gtk.Window(title="Molcajete Player")
        self._window.set_default_size(960, 540)

        if Gdk is not None:
            css_provider = Gtk.CssProvider()
            css_provider.load_from_data(b"window, drawingarea { background-color: #000000; }")
            screen = Gdk.Screen.get_default()
            if screen is not None:
                Gtk.StyleContext.add_provider_for_screen(
                    screen,
                    css_provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
                )

        if self.video_sink is not None and hasattr(self.video_sink.props, "widget"):
            self._window.add(self.video_sink.props.widget)
            self._window.connect("destroy", lambda w: self.stop())
            self._window.show_all()
            return True

        if self._use_x11_overlay:
            self._drawing_area = Gtk.DrawingArea()
            self._drawing_area.set_hexpand(True)
            self._drawing_area.set_vexpand(True)
            self._window.add(self._drawing_area)
            self._window.show_all()

            gdk_window = self._drawing_area.get_window()
            if gdk_window is not None and hasattr(gdk_window, "get_xid"):
                self._window_xid = gdk_window.get_xid()
                _set_element_window_handle(self.pipeline, self._window_xid)
                _set_element_window_handle(self.video_sink, self._window_xid)
                self._window.connect("destroy", lambda w: self.stop())
                return True

        self._fallback_to_autovideosink()
        return False

    def _on_shutdown_signal(self, signame: str) -> bool:
        logger.info("Signal %s received. Stopping the player...", signame)
        self.stop()
        return False

    def _install_signal_handlers(self) -> None:
        """Hace que SIGINT/SIGTERM/SIGHUP detengan el reproductor de forma ordenada desde el bucle principal."""
        if threading.current_thread() is not threading.main_thread() or not hasattr(GLib, "unix_signal_add"):
            return
        for signame in _SHUTDOWN_SIGNALS:
            signum = getattr(signal, signame, None)
            if signum is None:
                continue
            source_id = GLib.unix_signal_add(GLib.PRIORITY_HIGH, int(signum), self._on_shutdown_signal, signame)
            self._signal_sources.append(source_id)

    def _remove_signal_handlers(self) -> None:
        for source_id in self._signal_sources:
            GLib.source_remove(source_id)
        self._signal_sources = []

    def run(self) -> None:
        """Ejecuta el bucle de eventos de la aplicacion de forma bloqueante.

        SIGINT (Ctrl+C), SIGTERM y SIGHUP (cerrar la terminal) detienen el reproductor
        por el mismo camino que cerrar la ventana: tuberia a NULL, descargas detenidas
        y ventana destruida.
        """
        self._is_running = True
        self._stopped = False
        self.downloader.resume()
        if self.prune_cache:
            self.downloader.prune_cache(self.playlist.items)

        self._install_signal_handlers()
        try:
            if self._init_gtk_window():
                self.play()
                Gtk.main()
            else:
                self.loop = GLib.MainLoop()
                self.play()
                self.loop.run()
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt. Stopping the player...")
        finally:
            self._remove_signal_handlers()
            self.stop()
