import argparse
import logging
import os
import sys
from pathlib import Path

from molcajete_player.env import sanitize_snap_env

# Debe ejecutarse antes de importar gi (a traves de molcajete_player.player).
sanitize_snap_env()

from molcajete_player import player as player_module  # noqa: E402
from molcajete_player.contract import ContractEnv, ContractError  # noqa: E402
from molcajete_player.controller import ContractController  # noqa: E402
from molcajete_player.downloader import MediaDownloader  # noqa: E402
from molcajete_player.loader import load_playlist_from_file  # noqa: E402
from molcajete_player.models import Playlist  # noqa: E402
from molcajete_player.player import MolcajetePlayer  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """Construye el parser de argumentos de la CLI ``molcajete-player``."""
    parser = argparse.ArgumentParser(
        prog="molcajete-player",
        description="Video player (no audio) built on GStreamer and JSON metadata.",
    )
    parser.add_argument(
        "metadata",
        type=str,
        nargs="?",
        help=(
            "Path to the JSON file with the metadata and playlist. Without it, the player starts "
            "in contract mode with the agent (MOLCAJETE_APP_* environment, see README)."
        ),
    )
    parser.add_argument(
        "--sink",
        choices=["auto", "gtk", "kms", "fbdev", "fakesink"],
        default="auto",
        help=(
            "Video output: 'auto' (automatic graphical output), 'gtk' (native Gtk window), "
            "'kms' (direct DRM, no X11/Wayland), 'fbdev' (framebuffer) or 'fakesink' (headless)."
        ),
    )
    parser.add_argument(
        "--loop",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Force (--loop) or disable (--no-loop) looping, overriding the JSON value.",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=None,
        help=(
            "Isolated cache directory for downloaded renditions "
            "(default ~/.cache/molcajete-player/media or MOLCAJETE_CACHE_DIR)."
        ),
    )
    parser.add_argument(
        "--clean-cache",
        action="store_true",
        help=(
            "Before starting, delete all the player's renditions and temporary files "
            "in the cache directory (other files are kept)."
        ),
    )
    parser.add_argument(
        "--no-prune-cache",
        dest="prune_cache",
        action="store_false",
        help="Do not delete, on start, cached renditions that are no longer in the playlist.",
    )
    parser.add_argument(
        "--media-root",
        type=str,
        default=None,
        help="Directory that must contain every 'local_path' in the JSON (relative paths are resolved against it).",
    )
    parser.add_argument(
        "--no-prefetch",
        dest="prefetch_next",
        action="store_false",
        help="Do not start downloading the next item's s3_url in advance.",
    )
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help=(
            "Development only: accept http:// downloads to any host (e.g. local Storage "
            "seen from a container, http://host.docker.internal). In production downloads are always https."
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging (DEBUG).",
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

    if args.metadata is None:
        try:
            env = ContractEnv.from_environ(os.environ)
        except ContractError as exc:
            logging.error("Cannot start in contract mode: %s", exc)
            return 1
        if env is None:
            parser.error("the metadata file is missing (or the agent's MOLCAJETE_APP_* environment)")
        return run_contract(env)

    metadata_path = Path(args.metadata).expanduser()
    if not metadata_path.is_file():
        logging.error("Metadata file not found: %s", metadata_path)
        return 1

    try:
        playlist = load_playlist_from_file(metadata_path, media_root=args.media_root)
    except (OSError, ValueError) as exc:
        logging.error("Could not process the JSON file: %s", exc)
        return 1
    if args.loop is not None:
        playlist.loop = args.loop

    logging.info(
        "Playlist loaded: '%s' with %d items (loop: %s). Output: %s",
        playlist.name,
        len(playlist.items),
        playlist.loop,
        args.sink,
    )

    if args.allow_insecure_http:
        logging.warning("http:// downloads allowed (--allow-insecure-http): development only.")
    downloader = MediaDownloader(cache_dir=args.cache_dir, allow_insecure_http=args.allow_insecure_http)
    if args.clean_cache:
        removed = downloader.clear_cache()
        logging.info("Cache cleared: %d files removed from %s.", removed, downloader.cache_dir)

    try:
        player = MolcajetePlayer(
            playlist=playlist,
            sink_type=args.sink,
            downloader=downloader,
            prune_cache=args.prune_cache,
            prefetch_next=args.prefetch_next,
        )
    except RuntimeError as exc:
        logging.error("Could not start the GStreamer player: %s", exc)
        return 2

    try:
        player.run()
    except Exception:
        logging.exception("Error while running the player.")
        return 3

    if player.has_fatal_error:
        logging.error("Playback stopped because no playlist item could be played.")
        return 4

    return 0



def run_contract(env: ContractEnv) -> int:
    """Modo contrato con el agente (ADR 0015 de molcajete-ng): sin opciones; el agente nos da el entorno y
    nos comunicamos por ``program.json``, ``control.json`` y ``status.json`` en ``MOLCAJETE_APP_DATA``.

    Returns:
        0 al detenerse (senal o ventana cerrada); 2 si GStreamer no esta disponible. No sale porque fallen los
        elementos: espera y reintenta, o espera un programa nuevo.
    """
    logging.info("Contract mode: data in %s, output %s.", env.app_data, env.sink_type)
    if env.dev_insecure_http:
        logging.warning("http:// downloads allowed (MOLCAJETE_DEV_INSECURE_HTTP): development only.")
    downloader = MediaDownloader(cache_dir=env.cache_dir, allow_insecure_http=env.dev_insecure_http)
    try:
        player = MolcajetePlayer(
            playlist=Playlist(name="", loop=True, items=[]),
            sink_type=env.sink_type,
            downloader=downloader,
            # Arranca sin lista (el programa llega despues por los ficheros del contrato): limpiar la cache contra una lista vacia
            # borraria todo lo descargado en cada arranque.
            prune_cache=False,
            keep_alive=True,
        )
    except RuntimeError as exc:
        logging.error("Could not start the GStreamer player: %s", exc)
        return 2

    controller = ContractController(env, player)
    # Dentro del bucle, cuando la ventana (si la hay) ya existe.
    player_module.GLib.idle_add(controller.start, player_module.GLib.timeout_add)
    try:
        player.run()
    except Exception:
        logging.exception("Error while running the player.")
        return 3
    return 0

if __name__ == "__main__":
    sys.exit(main())
