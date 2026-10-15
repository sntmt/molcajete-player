from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class MediaItem:
    """Representa un elemento multimedia individual para reproduccion."""

    id: str
    title: str
    remote_url: Optional[str] = None
    download_url: Optional[str] = None
    local_path: Optional[str] = None
    duration: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_effective_uri(self) -> str:
        """Determina la URI efectiva a reproducir.

        Prioriza el archivo local si ya se encuentra descargado en disco.
        En caso contrario, recurre a la URL remota provista por el
        servicio cloud de streaming o transcodificacion.
        """
        if self.local_path:
            path = Path(self.local_path).expanduser()
            if path.is_file():
                return path.resolve().as_uri()

        if self.remote_url:
            return self.remote_url

        if self.local_path:
            return Path(self.local_path).expanduser().resolve().as_uri()

        raise ValueError(
            f"El elemento multimedia '{self.id}' no dispone de local_path ni remote_url valido."
        )

    def get_download_url(self) -> Optional[str]:
        """Obtiene la URL directa de descarga del archivo original.

        Prioriza 'download_url'. Si no esta presente pero 'remote_url'
        apunta a un archivo directamente descargable (no manifiesto .m3u8),
        se recurre a 'remote_url'.
        """
        if self.download_url:
            return self.download_url

        if self.remote_url and not self.remote_url.lower().endswith(".m3u8"):
            return self.remote_url

        return None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaItem":
        if "id" not in data or "title" not in data:
            raise ValueError("Cada elemento multimedia debe contener al menos 'id' y 'title'.")

        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            remote_url=data.get("remote_url"),
            download_url=data.get("download_url"),
            local_path=data.get("local_path"),
            duration=float(data["duration"]) if data.get("duration") is not None else None,
            metadata=data.get("metadata", {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "remote_url": self.remote_url,
            "download_url": self.download_url,
            "local_path": self.local_path,
            "duration": self.duration,
            "metadata": self.metadata,
        }


@dataclass
class Playlist:
    """Coleccion de elementos multimedia para reproduccion orquestada."""

    name: str
    loop: bool = False
    items: List[MediaItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Playlist":
        if "name" not in data:
            raise ValueError("La lista de reproduccion debe especificar un campo 'name'.")

        items_raw = data.get("items", [])
        if not isinstance(items_raw, list):
            raise ValueError("El campo 'items' debe ser una lista de elementos multimedia.")

        items = [MediaItem.from_dict(item) for item in items_raw]
        return cls(
            name=str(data["name"]),
            loop=bool(data.get("loop", False)),
            items=items,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "loop": self.loop,
            "items": [item.to_dict() for item in self.items],
        }
