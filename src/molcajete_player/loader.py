import json
from pathlib import Path
from typing import Optional, Union

from molcajete_player.models import Playlist


def load_playlist_from_string(content: str) -> Playlist:
    """Carga y valida una lista de reproduccion desde una cadena JSON.

    Raises:
        ValueError: Si la cadena no es JSON valido o no cumple con la estructura esperada.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Error al decodificar la fuente JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("El objeto raiz del JSON debe ser un diccionario.")

    return Playlist.from_dict(data)


def load_playlist_from_file(
    path: Union[str, Path],
    media_root: Optional[Union[str, Path]] = None,
) -> Playlist:
    """Carga y valida una lista de reproduccion desde un archivo JSON.

    Args:
        path: Ruta al archivo ``.json``.
        media_root: Si se indica, todos los ``local_path`` deben quedar dentro de este
            directorio (las rutas relativas se resuelven respecto a el).

    Raises:
        FileNotFoundError: Si la ruta indicada no existe o no es un archivo.
        ValueError: Si el contenido no cumple con la estructura esperada o algun
            ``local_path`` queda fuera de ``media_root``.
    """
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise FileNotFoundError(f"No se encontro el archivo de metadatos: {file_path}")

    playlist = load_playlist_from_string(file_path.read_text(encoding="utf-8"))
    if media_root is not None:
        playlist.confine_local_paths(media_root)
    return playlist


def load_playlist_from_json(source: Union[str, Path]) -> Playlist:
    """Carga una lista desde una ruta de archivo existente o, si no existe, desde una cadena JSON.

    Se conserva por compatibilidad. Prefiera ``load_playlist_from_file`` o
    ``load_playlist_from_string``: con esta funcion, una ruta mal escrita produce
    un error de decodificacion JSON en lugar de ``FileNotFoundError``.

    Raises:
        ValueError: Si el contenido no cumple con la estructura esperada de la lista.
    """
    if isinstance(source, Path) or Path(str(source)).expanduser().is_file():
        return load_playlist_from_file(source)
    return load_playlist_from_string(str(source))
