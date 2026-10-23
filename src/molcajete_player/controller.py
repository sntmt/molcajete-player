"""Contrato con el agente (ADR 0015): vigila ``program.json`` y ``control.json`` y mantiene ``status.json``.

Corre en el bucle principal de GLib/Gtk del reproductor (``start`` lo programa), asi que puede tocar la
tuberia sin hilos. Se comprueban los ficheros cada segundo por su fecha y tamano: es simple, no depende de
inotify y aguanta que el agente reescriba un fichero mientras el reproductor esta caido.
"""

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from molcajete_player.contract import (
    ContractEnv,
    ProgramError,
    program_to_playlist,
    read_control_playback,
    read_json,
    status_document,
    write_json_atomic,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_MS = 1000
# Aunque nada cambie, status.json se reescribe con esta frecuencia: su updated_at dice al agente que seguimos vivos.
HEARTBEAT_SECONDS = 30.0

Signature = Optional[Tuple[int, int]]


def _signature(path: Path) -> Signature:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


class ContractController:
    def __init__(self, env: ContractEnv, player: Any, clock: Optional[Callable[[], float]] = None):
        import time

        self.env = env
        self.player = player
        self._clock = clock or time.monotonic
        self.program_hash: Optional[str] = None
        self.rejected: Optional[Dict[str, Any]] = None
        self._program_sig: Signature = None
        self._control_sig: Signature = None
        self._requested_playback: Optional[str] = None
        self._last_status: Optional[Dict[str, Any]] = None
        self._last_write = float("-inf")

    def start(self, schedule: Callable[[int, Callable[[], bool]], Any]) -> bool:
        """Aplica lo que haya ya en los ficheros del contrato y programa las comprobaciones (``schedule`` = ``GLib.timeout_add``).

        Devuelve False para poder usarse directamente con ``GLib.idle_add``.
        """
        self.tick()
        schedule(POLL_INTERVAL_MS, self.tick)
        return False

    def tick(self) -> bool:
        self._check_control()
        self._check_program()
        self._write_status()
        return True

    def _check_control(self) -> None:
        signature = _signature(self.env.control_path)
        if signature == self._control_sig and self._requested_playback is not None:
            return
        self._control_sig = signature
        playback = read_control_playback(self.env.control_path)
        if playback == self._requested_playback:
            return
        self._requested_playback = playback
        logger.info("The agent requests %s.", playback)
        if playback == "paused":
            self.player.pause()
        else:
            self.player.play()

    def _check_program(self) -> None:
        signature = _signature(self.env.program_path)
        if signature == self._program_sig:
            return
        self._program_sig = signature
        if signature is None:
            return
        try:
            program = read_json(self.env.program_path)
        except (OSError, ValueError) as exc:
            self._reject(None, f"program.json is unreadable: {exc}")
            return
        candidate = program.get("program_hash") if isinstance(program, dict) else None
        candidate = candidate if isinstance(candidate, str) and candidate else None
        # El mismo programa reescrito no reinicia la reproduccion (cortaria el video en curso).
        if candidate is not None and candidate == self.program_hash:
            return
        try:
            program_hash, playlist = program_to_playlist(program, allow_http=self.env.dev_insecure_http)
        except ProgramError as exc:
            self._reject(candidate, str(exc))
            return
        logger.info("Program %s accepted (%d items).", program_hash, len(playlist.items))
        self.program_hash = program_hash
        self.rejected = None
        self.player.load_playlist(playlist)

    def _reject(self, program_hash: Optional[str], reason: str) -> None:
        # Se sigue con el programa anterior: program_hash no cambia y el motivo va aparte.
        logger.error("Program %s rejected: %s", program_hash or "(no program_hash)", reason)
        self.rejected = {"program_hash": program_hash, "reason": reason}

    def _write_status(self) -> None:
        document = status_document(self.program_hash, self.rejected, self.player.playback_state())
        content = {key: value for key, value in document.items() if key != "updated_at"}
        now = self._clock()
        if content == self._last_status and now - self._last_write < HEARTBEAT_SECONDS:
            return
        try:
            write_json_atomic(self.env.status_path, document)
        except OSError as exc:
            logger.error("Could not write %s: %s", self.env.status_path, exc)
            return
        self._last_status = content
        self._last_write = now
