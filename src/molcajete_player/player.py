import logging
import threading
from typing import Callable, Optional

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
            sink_type: Tipo de salida de video ('auto', 'kms', 'fbdev', 'fakesink').
                       'auto': autovideosink para entornos locales con servidor grafico.
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
        self.loop: Optional[GLib.MainLoop] = None
        self.loop_thread: Optional[threading.Thread] = None
        self._is_running = False

        self._build_pipeline()

    def _create_video_sink(self) -> Optional[Gst.Element]:
        """Crea el elemento de salida de video segun la configuracion de entorno."""
        sink_map = {
            "auto": "autovideosink",
            "kms": "kmssink",
            "fbdev": "fbdevsink",
            "fakesink": "fakesink",
        }

        element_name = sink_map.get(self.sink_type, "autovideosink")
        sink = Gst.ElementFactory.make(element_name, "video-sink")

        if sink is None and self.sink_type != "auto":
            logger.warning(
                "No fue posible instanciar el elemento '%s'. Recurriendo a 'autovideosink'.",
                element_name,
            )
            sink = Gst.ElementFactory.make("autovideosink", "video-sink")

        return sink

    def _build_pipeline(self) -> None:
        """Configura la tuberia playbin y conecta los manejadores del bus de mensajes."""
        self.pipeline = Gst.ElementFactory.make("playbin", "molcajete-playbin")
        if not self.pipeline:
            raise RuntimeError("No se pudo crear el elemento 'playbin' de GStreamer.")

        sink = self._create_video_sink()
        if sink is not None:
            self.pipeline.set_property("video-sink", sink)

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
            msg = f"Error en tuberia: {err.message}. Detalle: {debug}"
            logger.error(msg)
            if self.on_error_callback:
                self.on_error_callback(msg)
            self._handle_eos()
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

    def _handle_eos(self) -> None:
        """Avanza al siguiente elemento o finaliza la reproduccion."""
        if not self.next():
            logger.info("Lista de reproduccion completada.")
            self.stop()
            if self.on_eos_callback:
                self.on_eos_callback()

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
            logger.error("No se pudo obtener la URI para el elemento '%s': %s", item.id, exc)
            return False

        logger.info(
            "Reproduciendo elemento [%d/%d] '%s' (%s) -> URI: %s",
            self.current_index + 1,
            len(self.playlist.items),
            item.title,
            item.id,
            uri,
        )

        self.pipeline.set_state(Gst.State.READY)
        self.pipeline.set_property("uri", uri)
        state_ret = self.pipeline.set_state(Gst.State.PLAYING)
        if state_ret == Gst.StateChangeReturn.FAILURE:
            logger.error("Fallo al cambiar la tuberia al estado PLAYING.")
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
        """Ejecuta el bucle de eventos GLib de forma bloqueante."""
        self.loop = GLib.MainLoop()
        self._is_running = True
        self.play()
        try:
            self.loop.run()
        except KeyboardInterrupt:
            logger.info("Interrupcion por teclado detectada. Deteniendo reproductor...")
        finally:
            self.stop()
