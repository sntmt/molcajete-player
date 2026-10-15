import argparse
import logging
import sys
from pathlib import Path

from molcajete_player.loader import load_playlist_from_json
from molcajete_player.player import MolcajetePlayer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="molcajete-player",
        description="Reproductor de contenido audiovisual basado en GStreamer y metadatos JSON.",
    )
    parser.add_argument(
        "metadata",
        type=str,
        help="Ruta al archivo JSON con los metadatos y lista de reproduccion.",
    )
    parser.add_argument(
        "--sink",
        choices=["auto", "gtk", "kms", "fbdev", "fakesink"],
        default="auto",
        help="Salida de video: 'auto' (interfaz grafica automatica), 'gtk' (ventana nativa Gtk), 'kms' (DRM directo sin X11/Wayland), 'fbdev' (framebuffer) o 'fakesink' (modo headless).",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Fuerza la reproduccion continua en bucle de toda la lista.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Activa el nivel de registro detallado (DEBUG).",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    metadata_path = Path(args.metadata).expanduser()
    if not metadata_path.is_file():
        logging.error("No se encontro el archivo de metadatos en: %s", metadata_path)
        return 1

    try:
        playlist = load_playlist_from_json(metadata_path)
        if args.loop:
            playlist.loop = True
    except Exception as exc:
        logging.error("Fallo al procesar el archivo JSON: %s", exc)
        return 1

    logging.info(
        "Lista cargada: '%s' con %d elementos (bucle: %s). Salida: %s",
        playlist.name,
        len(playlist.items),
        playlist.loop,
        args.sink,
    )

    try:
        player = MolcajetePlayer(playlist=playlist, sink_type=args.sink)
    except RuntimeError as exc:
        logging.error("No se pudo iniciar el reproductor GStreamer: %s", exc)
        return 2

    try:
        player.run()
    except Exception as exc:
        logging.error("Error durante la ejecucion del reproductor: %s", exc)
        return 3

    if player.has_fatal_error:
        logging.error("La reproduccion se detuvo porque ningun elemento de la lista pudo reproducirse.")
        return 4

    return 0


if __name__ == "__main__":
    sys.exit(main())
