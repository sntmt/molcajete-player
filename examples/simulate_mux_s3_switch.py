#!/usr/bin/env python3
"""Simulacion end-to-end del flujo Mux (stream) -> S3 (rendition) -> Disco local.

Este script:
1. Genera dos videos cortos (~3s) con GStreamer:
   - Video "stream" (patron de barras SMPTE) que simula el stream remoto de Mux.
   - Video "rendition" (patron de pelota en movimiento) que simula el rendition en S3.
2. Levanta un servidor HTTP local que simula:
   - /mux/stream.mp4 -> disponible inmediatamente.
   - /s3/rendition.mp4 -> devuelve 404 en el primer intento (simulando que Mux aun
     esta generando el rendition) y 200 OK a partir del segundo intento.
3. Arranca MolcajetePlayer con loop=True:
   - Vuelta 1: El archivo no esta en disco -> reproduce desde http://.../mux/stream.mp4
     mientras descarga en segundo plano desde http://.../s3/rendition.mp4.
   - Vueltas siguientes: playbin precarga la siguiente URI unos segundos antes del final
     (about-to-finish). En cuanto el rendition esta verificado en disco, la vuelta
     precargada usa file:///.../device_cache/<id>_<hash16>.mp4 y la simulacion termina.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import logging
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

from molcajete_player.env import sanitize_snap_env

# Debe ejecutarse antes de importar gi (a traves de molcajete_player.player).
sanitize_snap_env()

from molcajete_player.downloader import MediaDownloader  # noqa: E402
from molcajete_player.models import MediaItem, Playlist  # noqa: E402
from molcajete_player.player import GLib, MolcajetePlayer  # noqa: E402

MAX_CYCLES = 5


def generate_test_mp4(output_path: Path, pattern: str, num_buffers: int = 90) -> None:
    """Genera un archivo MP4 corto de prueba usando gst-launch-1.0."""
    cmd = [
        "gst-launch-1.0",
        "-q",
        "-e",
        "videotestsrc",
        f"num-buffers={num_buffers}",
        f"pattern={pattern}",
        "!",
        "video/x-raw,width=640,height=360,framerate=30/1",
        "!",
        "videoconvert",
        "!",
        "x264enc",
        "tune=zerolatency",
        "!",
        "mp4mux",
        "!",
        "filesink",
        f"location={output_path}",
    ]
    subprocess.run(cmd, check=True)


def run_simulation(sink_type: str = "auto") -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("simulacion")

    with tempfile.TemporaryDirectory(prefix="molcajete_sim_") as tmpdir:
        tmp_path = Path(tmpdir)
        server_dir = tmp_path / "cloud"
        client_dir = tmp_path / "device_cache"
        server_dir.mkdir()
        client_dir.mkdir()

        mux_video = server_dir / "mux_stream.mp4"
        s3_video = server_dir / "s3_rendition.mp4"

        logger.info("Generando videos de prueba con GStreamer (~3s cada uno)...")
        generate_test_mp4(mux_video, pattern="smpte", num_buffers=90)
        generate_test_mp4(s3_video, pattern="ball", num_buffers=90)
        s3_sha256 = hashlib.sha256(s3_video.read_bytes()).hexdigest()

        s3_requests = {"count": 0}

        class CloudMockHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed_path = urllib.parse.urlsplit(self.path).path
                if parsed_path == "/mux/stream.mp4":
                    data = mux_video.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif parsed_path == "/s3/rendition.mp4":
                    s3_requests["count"] += 1
                    if s3_requests["count"] == 1:
                        # Primera consulta: simulamos que Mux aun no ha subido el rendition a S3
                        self.send_response(404)
                        self.end_headers()
                    else:
                        data = s3_video.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", "video/mp4")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format, *args):
                pass

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CloudMockHandler)
        port = httpd.server_port
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        mux_url = f"http://127.0.0.1:{port}/mux/stream.mp4"
        s3_url = (
            f"http://127.0.0.1:{port}/s3/rendition.mp4"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=sim-secret-sig"
        )

        item = MediaItem(
            id="demo-mux-s3",
            title="Video Usuario (Mux Stream -> S3 Disco Seguro)",
            remote_url=mux_url,
            s3_url=s3_url,
            sha256=s3_sha256,
        )
        playlist = Playlist(name="Simulacion Mux a Disco", loop=True, items=[item])
        downloader = MediaDownloader(
            cache_dir=client_dir,
            retry_interval=1.0,
            max_retries=10,
        )

        uris_played = []

        def on_item_start(index: int, started_item: MediaItem, uri: str) -> None:
            uris_played.append(uri)
            logger.info(">>> [CICLO %d] Fuente seleccionada: %s", len(uris_played), uri)
            # Con el cambio sin huecos (about-to-finish), la URI del ciclo siguiente se decide
            # unos segundos antes del final del actual, asi que el archivo descargado puede
            # aparecer uno o dos ciclos despues del primero.
            if uri.startswith("file://") or len(uris_played) >= MAX_CYCLES:
                logger.info("Simulacion completada. Deteniendo...")
                GLib.idle_add(player.stop)

        player = MolcajetePlayer(
            playlist=playlist,
            sink_type=sink_type,
            downloader=downloader,
            on_item_start=on_item_start,
        )

        try:
            player.run()
        finally:
            httpd.shutdown()

        logger.info("Resumen de URIs reproducidas por orden: %s", uris_played)
        switched = any(uri.startswith("file://") for uri in uris_played[1:])
        if uris_played and uris_played[0].startswith("http://") and switched:
            logger.info("EXITO: Se inicio reproduciendo el stream y se conmuto al archivo en disco tras descargar de S3.")
            return 0
        logger.error("FALLO: No se produjo la transicion esperada de http:// a file://")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simula el paso de stream Mux a archivo local desde S3.")
    parser.add_argument(
        "--sink",
        choices=["auto", "gtk", "kms", "fbdev", "fakesink"],
        default="auto",
        help="Usa 'auto' para ver la ventana con el cambio visual (barras -> pelota) o 'fakesink' sin ventana.",
    )
    args = parser.parse_args()
    sys.exit(run_simulation(sink_type=args.sink))
