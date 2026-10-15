import json
from pathlib import Path
from typing import Union

from molcajete_player.models import Playlist


def load_playlist_from_json(source: Union[str, Path]) -> Playlist:
    """Carga y valida una lista de reproduccion desde una ruta de archivo JSON o cadena JSON.

    Args:
        source: Ruta a un archivo .json existente o cadena con formato JSON valido.

    Returns:
        Instancia validada de Playlist.

    Raises:
        FileNotFoundError: Si la ruta indicada no existe en el sistema.
        ValueError: Si el contenido no cumple con la estructura esperada de la lista.
    """
    path_candidate = Path(str(source)).expanduser()
    if path_candidate.is_file():
        content = path_candidate.read_text(encoding="utf-8")
    else:
        content = str(source)

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Error al decodificar la fuente JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("El objeto raiz del JSON debe ser un diccionario.")

    return Playlist.from_dict(data)
