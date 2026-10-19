import argparse
import logging
import sys
from pathlib import Path

from molcajete_player.env import sanitize_snap_env

# Debe ejecutarse antes de importar gi (a traves de molcajete_player.player).
sanitize_snap_env()

from molcajete_player.downloader import MediaDownloader  # noqa: E402
from molcajete_player.loader import load_playlist_from_file  # noqa: E402
from molcajete_player.player import MolcajetePlayer  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """Construye el parser de argumentos de la CLI ``molcajete-player``."""
    parser = argparse.ArgumentParser(
        prog="molcajete-player",
        description="Reproductor de video (sin audio) basado en GStreamer y metadatos JSON.",
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
        help=(
            "Salida de video: 'auto' (interfaz grafica automatica), 'gtk' (ventana nativa Gtk), "
            "'kms' (DRM directo sin X11/Wayland), 'fbdev' (framebuffer) o 'fakesink' (modo headless)."
        ),
    )
    parser.add_argument(
        "--loop",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Fuerza (--loop) o desactiva (--no-loop) la reproduccion en bucle, sobreescribiendo el valor del JSON.",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=None,
        help=(
            "Directorio aislado de cache para almacenar renditions descargados desde S3 "
            "(por defecto ~/.cache/molcajete-player/media o MOLCAJETE_CACHE_DIR)."
        ),
    )
    parser.add_argument(
        "--clean-cache",
        action="store_true",
        help=(
            "Elimina antes de empezar todos los renditions y temporales del reproductor "
            "en el directorio de cache (los demas archivos se conservan)."
        ),
    )
    parser.add_argument(
        "--no-prune-cache",
        dest="prune_cache",
        action="store_false",
        help="No elimina al arrancar los renditions en cache que ya no pertenecen a la lista.",
    )
    parser.add_argument(
        "--media-root",
        type=str,
        default=None,
        help="Directorio dentro del cual deben estar todos los 'local_path' del JSON (las rutas relativas se resuelven respecto a el).",
    )
    parser.add_argument(
        "--no-prefetch",
        dest="prefetch_next",
        action="store_false",
        help="No inicia por adelantado la descarga del s3_url del siguiente elemento.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Activa el nivel de registro detallado (DEBUG).",
    )
    return parser


def main() -> int:
    """Punto de entrada de la CLI.

    Returns:
        0 si la ejecucion termina correctamente; 1 si el JSON no existe o es
        invalido; 2 si GStreamer no esta disponible o no se pudo crear la
        tuberia; 3 ante un error inesperado en el bucle principal; 4 si todos
        los elementos de la lista fallaron consecutivamente.
    """
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
        playlist = load_playlist_from_file(metadata_path, media_root=args.media_root)
    except (OSError, ValueError) as exc:
        logging.error("Fallo al procesar el archivo JSON: %s", exc)
        return 1
    if args.loop is not None:
        playlist.loop = args.loop

    logging.info(
        "Lista cargada: '%s' con %d elementos (bucle: %s). Salida: %s",
        playlist.name,
        len(playlist.items),
        playlist.loop,
        args.sink,
    )

    downloader = MediaDownloader(cache_dir=args.cache_dir)
    if args.clean_cache:
        removed = downloader.clear_cache()
        logging.info("Cache limpiada: %d archivos eliminados de %s.", removed, downloader.cache_dir)

    try:
        player = MolcajetePlayer(
            playlist=playlist,
            sink_type=args.sink,
            downloader=downloader,
            prune_cache=args.prune_cache,
            prefetch_next=args.prefetch_next,
        )
    except RuntimeError as exc:
        logging.error("No se pudo iniciar el reproductor GStreamer: %s", exc)
        return 2

    try:
        player.run()
    except Exception:
        logging.exception("Error durante la ejecucion del reproductor.")
        return 3

    if player.has_fatal_error:
        logging.error("La reproduccion se detuvo porque ningun elemento de la lista pudo reproducirse.")
        return 4

    return 0


if __name__ == "__main__":
    sys.exit(main())
