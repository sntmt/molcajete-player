"""Contrato con el agente del dispositivo (ADR 0015 de molcajete-ng).

El agente no sabe nada del reproductor: lo arranca con un entorno estandar y se comunican por tres ficheros JSON en
``MOLCAJETE_APP_DATA``. El agente escribe ``program.json`` (el programa tal cual llega de la plataforma) y
``control.json`` (lo que se pide: reproducir o pausar); el reproductor escribe ``status.json`` (lo que hace de verdad).
Todo lo que es del reproductor vive aqui: traducir y validar el programa, y el formato del estado.

Este modulo no depende de GStreamer: se prueba sin el.
"""

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlparse

from molcajete_player.models import MediaItem, Playlist

CONTRACT_VERSION = "1"
SUPPORTED_MANIFEST_VERSIONS = (2,)

PROGRAM_FILE = "program.json"
CONTROL_FILE = "control.json"
STATUS_FILE = "status.json"

# Salida de video que elige el agente (conoce el hardware) -> sink del reproductor.
_VIDEO_OUTPUTS = {"kms": "kms", "gtk": "gtk", "fakesink": "fakesink"}


class ContractError(ValueError):
    """El entorno del contrato no es valido (el agente no puede arrancarnos asi)."""


class ProgramError(ValueError):
    """El programa no se puede reproducir; el motivo va a ``status.json``."""


@dataclass(frozen=True)
class ContractEnv:
    """Lo que el agente nos da al arrancar: carpetas, salida de video y, en desarrollo, si se admite http.

    ``inbox`` es donde el agente deja ``program.json`` y ``control.json`` (solo el la escribe; nosotros solo
    leemos); ``app_data`` es nuestra (``status.json`` y la cache). Sin ``inbox``, todo va en ``app_data``.
    """

    app_data: Path
    video_output: str
    dev_insecure_http: bool = False
    inbox: Optional[Path] = None

    @property
    def program_path(self) -> Path:
        return (self.inbox or self.app_data) / PROGRAM_FILE

    @property
    def control_path(self) -> Path:
        return (self.inbox or self.app_data) / CONTROL_FILE

    @property
    def status_path(self) -> Path:
        return self.app_data / STATUS_FILE

    @property
    def cache_dir(self) -> Path:
        return self.app_data / "cache"

    @property
    def sink_type(self) -> str:
        return _VIDEO_OUTPUTS.get(self.video_output, "auto")

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Optional["ContractEnv"]:
        """El entorno del contrato, o ``None`` si no nos arranca un agente (uso por linea de comandos).

        Raises:
            ContractError: si nos arranca un agente con un contrato que no entendemos o sin carpeta de datos.
        """
        app_data = environ.get("MOLCAJETE_APP_DATA")
        version = environ.get("MOLCAJETE_APP_CONTRACT")
        if not app_data and not version:
            return None
        if version != CONTRACT_VERSION:
            raise ContractError(f"Unsupported contract version: {version!r} (expected {CONTRACT_VERSION!r}).")
        if not app_data:
            raise ContractError("MOLCAJETE_APP_DATA is missing.")
        inbox = environ.get("MOLCAJETE_APP_INBOX")
        return cls(
            app_data=Path(app_data),
            video_output=environ.get("MOLCAJETE_VIDEO_OUTPUT", ""),
            dev_insecure_http=environ.get("MOLCAJETE_DEV_INSECURE_HTTP") == "1",
            inbox=Path(inbox) if inbox else None,
        )


def _url(clip: Dict[str, Any], key: str, index: int, schemes: Tuple[str, ...]) -> Optional[str]:
    value = clip.get(key)
    if value is None:
        return None
    parsed = urlparse(value) if isinstance(value, str) else None
    if parsed is None or parsed.scheme not in schemes or not parsed.netloc:
        raise ProgramError(f"clips[{index}].{key} must be a {' or '.join(schemes)} URL.")
    return value


def _item(clip: Any, index: int, schemes: Tuple[str, ...]) -> MediaItem:
    if not isinstance(clip, dict):
        raise ProgramError(f"clips[{index}] is not an object.")
    clip_id = clip.get("clip_id")
    if isinstance(clip_id, bool) or not isinstance(clip_id, (str, int)) or str(clip_id) == "":
        raise ProgramError(f"clips[{index}].clip_id is missing or invalid.")

    remote_url = _url(clip, "stream_url", index, schemes)
    download_url = _url(clip, "download_url", index, schemes)
    if remote_url is None and download_url is None:
        raise ProgramError(f"clips[{index}] has neither stream_url nor download_url.")

    duration = clip.get("duration_s")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0):
        raise ProgramError(f"clips[{index}].duration_s must be a positive number.")

    title = clip.get("title")
    # Solo estos campos: nada de la plataforma acaba en local_path (rutas que abriria el reproductor).
    return MediaItem(
        id=str(clip_id),
        title=title if isinstance(title, str) and title else str(clip_id),
        remote_url=remote_url,
        s3_url=download_url,
        duration=float(duration) if duration is not None else None,
    )


def program_to_playlist(program: Any, allow_http: bool = False) -> Tuple[str, Playlist]:
    """Traduce el programa de la plataforma (payload de ``deploy_program``) a nuestra lista.

    Returns:
        ``(program_hash, playlist)``. La lista va siempre en bucle.

    Raises:
        ProgramError: si el programa no se puede reproducir (con el motivo).
    """
    if not isinstance(program, dict):
        raise ProgramError("The program is not a JSON object.")
    program_hash = program.get("program_hash")
    if not isinstance(program_hash, str) or not program_hash:
        raise ProgramError("program_hash is missing or invalid.")
    manifest = program.get("manifest")
    if not isinstance(manifest, dict):
        raise ProgramError("manifest is missing or invalid.")
    version = manifest.get("version")
    if version not in SUPPORTED_MANIFEST_VERSIONS:
        raise ProgramError(f"Unsupported manifest version: {version!r}.")
    clips = manifest.get("clips")
    if not isinstance(clips, list):
        raise ProgramError("manifest.clips is missing or invalid.")

    schemes = ("https", "http") if allow_http else ("https",)
    items = [_item(clip, index, schemes) for index, clip in enumerate(clips)]
    seen = set()
    for item in items:
        if item.id in seen:
            raise ProgramError(f"Duplicate clip_id: {item.id}.")
        seen.add(item.id)
    return program_hash, Playlist(name=program_hash, loop=True, items=items)


def read_json(path: Path) -> Any:
    """Contenido de un fichero del contrato, o ``None`` si no existe.

    Raises:
        ValueError: si existe pero no es JSON valido.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return json.loads(text)


def read_control_playback(path: Path) -> str:
    """``playing`` o ``paused`` segun ``control.json``; sin fichero (o ilegible), ``playing``."""
    try:
        control = read_json(path)
    except (OSError, ValueError):
        return "playing"
    playback = control.get("playback") if isinstance(control, dict) else None
    return "paused" if playback == "paused" else "playing"


def status_document(
    program_hash: Optional[str],
    rejected: Optional[Dict[str, Any]],
    playback: Optional[str],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """El contenido de ``status.json``: lo unico que el agente informa a la plataforma.

    ``program_hash`` es el programa que se esta reproduciendo (el ultimo aceptado). Si se rechazo uno
    posterior, ``error`` dice cual y por que (``{"program_hash", "reason"}``) y se sigue con el anterior:
    asi el agente puede acusar el rechazo sin que la web crea que el dispositivo tiene el programa nuevo.
    """
    moment = now or datetime.now(timezone.utc)
    return {
        "program_hash": program_hash,
        "error": rejected,
        "playback": playback,
        "updated_at": moment.isoformat(timespec="seconds"),
    }


def write_json_atomic(path: Path, document: Dict[str, Any]) -> None:
    """Escribe sin que el agente pueda leer el fichero a medias: temporal en la misma carpeta y ``rename``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(document, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
