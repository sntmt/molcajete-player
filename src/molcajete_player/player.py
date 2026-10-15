from __future__ import annotations

import logging
import sys
import threading
from typing import Any, Callable, Optional

from molcajete_player.models import MediaItem, Playlist

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

# Intentar importar Gtk para entornos de escritorio con ventana nativa (Linux / macOS)
try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    GTK_AVAILABLE = True
except (ImportError, ValueError):
    GTK_AVAILABLE = False
    Gtk = None


class MolcajetePlayer:
    """Controlador de reproduccion multimedia basado en GStreamer para Linux y entornos locales."""

    def __init__(
        self,
        playlist: Playlist,
        sink_type: str = "auto",
        on_eos: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
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
        """
        if not GST_AVAILABLE:
            raise RuntimeError(
                f"GStreamer no se encuentra disponible en este entorno: {_GST_IMPORT_ERROR}. "
                "Verifique la instalacion de PyGObject y las librerias del sistema."
            )

        if not Gst.is_initialized():
            Gst.init(None)

        self.playlist = playlist
        self.sink_type = sink_type.lower()
        self.on_eos_callback = on_eos
        self.on_error_callback = on_error

        self.current_index = 0
        self.pipeline: Optional[Gst.Element] = None
        self.video_sink: Optional[Gst.Element] = None
        self.loop: Optional[GLib.MainLoop] = None
        self.loop_thread: Optional[threading.Thread] = None
        self._is_running = False
        self._consecutive_errors = 0
        self.has_fatal_error = False
        self._use_gtk = False
        self._window: Optional[Any] = None

        self._build_pipeline()

    def _create_video_sink(self) -> Optional[Gst.Element]:
        """Crea el elemento de salida de video segun la configuracion de entorno."""
        if self.sink_type == "fakesink":
            return Gst.ElementFactory.make("fakesink", "video-sink")
        if self.sink_type == "kms":
            return Gst.ElementFactory.make("kmssink", "video-sink")
        if self.sink_type == "fbdev":
            return Gst.ElementFactory.make("fbdevsink", "video-sink")

        # Para entornos graficos ('auto' o 'gtk'):
        # Si GTK 3 y gtksink estan disponibles, se utiliza gtksink embebido en ventana nativa Gtk.
        # Esto soluciona la renderizacion tanto en macOS (Quartz) como en Linux (Wayland / X11).
        if GTK_AVAILABLE and self.sink_type in ("auto", "gtk"):
            gtksink = Gst.ElementFactory.make("gtksink", "video-sink")
            if gtksink is not None:
                self._use_gtk = True
                return gtksink

        return Gst.ElementFactory.make("autovideosink", "video-sink")

    def _build_pipeline(self) -> None:
        """Configura la tuberia playbin y conecta los manejadores del bus de mensajes."""
        self.pipeline = Gst.ElementFactory.make("playbin", "molcajete-playbin")
        if not self.pipeline:
            raise RuntimeError("No se pudo crear el elemento 'playbin' de GStreamer.")

        self.video_sink = self._create_video_sink()
        if self.video_sink is not None:
            self.pipeline.set_property("video-sink", self.video_sink)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        """Procesa mensajes emitidos por el bus de GStreamer."""
        t = message.type
        if t == Gst.MessageType.EOS:
            logger.info("Fin de reproduccion alcanzado (EOS).")
            self._handle_eos()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            current_item = (
                self.playlist.items[self.current_index]
                if 0 <= self.current_index < len(self.playlist.items)
                else None
            )
            item_desc = f"'{current_item.title}' ({current_item.id})" if current_item else "desconocido"
            msg = f"Error en elemento {item_desc}: {err.message}. Detalle: {debug}"
            logger.error(msg)
            self._handle_error(msg)
        elif t == Gst.MessageType.BUFFERING:
            percent = message.parse_buffering()
            logger.debug("Almacenando bufer de transmision de red: %d%%", percent)
            if percent < 100:
                self.pipeline.set_state(Gst.State.PAUSED)
            else:
                self.pipeline.set_state(Gst.State.PLAYING)
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                old_state, new_state, _ = message.parse_state_changed()
                logger.debug("Cambio de estado en tuberia: %s -> %s", old_state.value_name, new_state.value_name)
                if new_state == Gst.State.PLAYING:
                    self._consecutive_errors = 0

    def _handle_eos(self) -> None:
        """Avanza al siguiente elemento o finaliza la reproduccion tras completar una pista con exito."""
        self._consecutive_errors = 0
        if not self.next():
            logger.info("Lista de reproduccion completada.")
            self.stop()
            if self.on_eos_callback:
                self.on_eos_callback()

    def _handle_error(self, error_message: str) -> None:
        """Gestiona un fallo en la reproduccion evitando bucles infinitos."""
        self._consecutive_errors += 1
        if self.on_error_callback:
            self.on_error_callback(error_message)

        total_items = len(self.playlist.items)
        if total_items == 0 or self._consecutive_errors >= total_items:
            logger.critical(
                "Todos los elementos de la lista de reproduccion (%d) fallaron consecutivamente. "
                "Deteniendo reproductor para evitar un bucle indefinido de errores.",
                total_items,
            )
            self.has_fatal_error = True
            self.stop()
            return

        logger.warning(
            "Fallo en el elemento actual (%d/%d fallos consecutivos). Pasando al siguiente elemento...",
            self._consecutive_errors,
            total_items,
        )
        if not self.next():
            logger.info("Fin de la lista alcanzado tras fallo de reproduccion.")
            self.stop()

    def play_item_at_index(self, index: int) -> bool:
        """Establece y reproduce el elemento correspondiente al indice indicado."""
        if not self.playlist.items:
            logger.warning("La lista de reproduccion no contiene elementos.")
            return False

        if index < 0 or index >= len(self.playlist.items):
            return False

        self.current_index = index
        item: MediaItem = self.playlist.items[self.current_index]

        try:
            uri = item.get_effective_uri()
        except ValueError as exc:
            msg = f"No se pudo obtener la URI para el elemento '{item.id}': {exc}"
            logger.error(msg)
            if GLib:
                GLib.idle_add(self._handle_error, msg)
            else:
                self._handle_error(msg)
            return False

        logger.info(
            "Reproduciendo elemento [%d/%d] '%s' (%s) -> URI: %s",
            self.current_index + 1,
            len(self.playlist.items),
            item.title,
            item.id,
            uri,
        )

        if self._window is not None and hasattr(self._window, "set_title"):
            self._window.set_title(f"Molcajete Player - {item.title}")

        self.pipeline.set_state(Gst.State.READY)
        self.pipeline.set_property("uri", uri)
        state_ret = self.pipeline.set_state(Gst.State.PLAYING)
        if state_ret == Gst.StateChangeReturn.FAILURE:
            msg = f"Fallo al cambiar la tuberia al estado PLAYING para el elemento '{item.id}'."
            logger.error(msg)
            if GLib:
                GLib.idle_add(self._handle_error, msg)
            else:
                self._handle_error(msg)
            return False

        return True

    def play(self) -> bool:
        """Inicia o reanuda la reproduccion."""
        if self.pipeline is None:
            return False

        current_state, _, _ = self.pipeline.get_state(0)
        if current_state == Gst.State.PAUSED:
            self.pipeline.set_state(Gst.State.PLAYING)
            return True

        return self.play_item_at_index(self.current_index)

    def pause(self) -> None:
        """Pausa la reproduccion actual."""
        if self.pipeline:
            self.pipeline.set_state(Gst.State.PAUSED)

    def stop(self) -> None:
        """Detiene la reproduccion y libera recursos de la tuberia."""
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)

        if self.loop and self.loop.is_running():
            self.loop.quit()

        if self._use_gtk and GTK_AVAILABLE and Gtk and Gtk.main_level() > 0:
            Gtk.main_quit()

        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None

        self._is_running = False

    def next(self) -> bool:
        """Avanza al siguiente elemento de la lista."""
        next_idx = self.current_index + 1
        if next_idx >= len(self.playlist.items):
            if self.playlist.loop:
                next_idx = 0
            else:
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

    def run(self) -> None:
        """Ejecuta el bucle de eventos de la aplicacion de forma bloqueante."""
        self._is_running = True

        if self._use_gtk and GTK_AVAILABLE and self.video_sink and hasattr(self.video_sink.props, "widget"):
            Gtk.init([])
            self._window = Gtk.Window(title="Molcajete Player")
            self._window.set_default_size(960, 540)
            self._window.add(self.video_sink.props.widget)
            self._window.connect("destroy", lambda w: self.stop())
            self._window.show_all()

            self.play()
            try:
                Gtk.main()
            except KeyboardInterrupt:
                logger.info("Interrupcion por teclado detectada. Deteniendo reproductor...")
            finally:
                self.stop()
        else:
            self.loop = GLib.MainLoop()
            self.play()
            try:
                self.loop.run()
            except KeyboardInterrupt:
                logger.info("Interrupcion por teclado detectada. Deteniendo reproductor...")
            finally:
                self.stop()
