import hashlib
import hmac
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

_ALLOWED_MEDIA_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".ts"}
_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_URL_WITH_QUERY_RE = re.compile(r"(https?://[^\s?#'\"]+)\?[^\s'\"]*")


def sanitize_media_id(raw_id: str) -> str:
    """Normaliza un identificador para su uso seguro en nombres de archivo sin riesgo de Path Traversal."""
    cleaned = _SAFE_ID_RE.sub("_", raw_id.strip()).strip("_-")
    return cleaned[:64] if cleaned else "media"


def canonicalize_url(url: str) -> str:
    """Elimina query strings y fragmentos (ej. firmas temporales de S3 Presigned URLs) para obtener una URL base estable."""
    parsed = urllib.parse.urlsplit(url.strip())
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}"


def extract_safe_extension(url_or_path: Optional[str], default: str = ".mp4") -> str:
    """Extrae una extension multimedia permitida ignorando parametros de consulta de URLs firmadas."""
    if not url_or_path:
        return default
    parsed_path = urllib.parse.urlsplit(url_or_path.strip()).path
    ext = Path(parsed_path).suffix.lower()
    if ext in _ALLOWED_MEDIA_EXTENSIONS:
        return ext
    return default


def redact_url_for_log(url: str) -> str:
    """Oculta parametros de consulta sensibles (como X-Amz-Signature en S3 Presigned URLs) para registro en logs."""
    parsed = urllib.parse.urlsplit(url.strip())
    if not parsed.query:
        return url
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?<redacted>"


def redact_urls_in_text(text: str) -> str:
    """Oculta los query strings de todas las URLs http(s) contenidas en un texto libre (p. ej. mensajes de GStreamer)."""
    return _URL_WITH_QUERY_RE.sub(r"\1?<redacted>", text)


def parse_bool(value: Any, field_name: str) -> bool:
    """Convierte un valor JSON en booleano aceptando solo ``true``/``false`` (o sus cadenas equivalentes)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError(f"El campo '{field_name}' debe ser booleano (true/false); se recibio {value!r}.")


@dataclass
class MediaItem:
    """Representa un elemento multimedia individual para reproduccion."""

    id: str
    title: str
    remote_url: Optional[str] = None
    s3_url: Optional[str] = None
    local_path: Optional[str] = None
    sha256: Optional[str] = None
    duration: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Ruta determinista en cache_dir asignada por MediaDownloader; no forma parte del JSON.
    cache_path: Optional[str] = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise ValueError("El identificador 'id' del elemento multimedia no puede estar vacio.")

        if not isinstance(self.metadata, dict):
            raise ValueError(f"El campo 'metadata' del elemento '{self.id}' debe ser un objeto JSON.")

        if self.local_path is not None:
            raw_parts = Path(self.local_path).parts
            if ".." in raw_parts:
                raise ValueError(
                    f"Ruta local insegura en '{self.id}': no se permite navegacion de directorios ('..')."
                )

        if self.sha256 is not None:
            normalized_hash = str(self.sha256).strip().lower()
            if normalized_hash.startswith("sha256:"):
                normalized_hash = normalized_hash.split(":", 1)[1].strip()
            if not _SHA256_RE.match(normalized_hash):
                raise ValueError(
                    f"El campo 'sha256' del elemento '{self.id}' debe ser un hash hexadecimal valido de 64 caracteres."
                )
            self.sha256 = normalized_hash

    def build_cache_filename(self) -> str:
        """Genera un nombre de archivo determinista y seguro libre de colisiones para el cache del kiosko."""
        safe_id = sanitize_media_id(self.id)
        if self.sha256:
            digest = self.sha256[:16]
        elif self.s3_url:
            canonical = canonicalize_url(self.s3_url)
            digest = hashlib.sha256(f"{self.id}:{canonical}".encode("utf-8")).hexdigest()[:16]
        else:
            digest = hashlib.sha256(self.id.encode("utf-8")).hexdigest()[:16]

        ext = extract_safe_extension(self.s3_url or self.local_path, default=".mp4")
        return f"{safe_id}_{digest}{ext}"

    def resolve_cache_path(self, cache_dir: Union[str, Path]) -> Path:
        """Resuelve la ruta absoluta confinada dentro de cache_dir impidiendo escapes de directorio."""
        base_dir = Path(cache_dir).expanduser().resolve()
        filename = self.build_cache_filename()
        target_path = (base_dir / filename).resolve()
        if not target_path.is_relative_to(base_dir):
            raise ValueError(
                f"La ruta resuelta '{target_path}' escapa del directorio de cache permitido '{base_dir}'."
            )
        return target_path

    def verify_integrity(self, file_path: Optional[Union[str, Path]] = None) -> bool:
        """Verifica que el archivo exista y, si se especifico sha256, coincida exactamente con su resumen criptografico."""
        target = file_path or self.local_path
        if not target:
            return False
        path = Path(target).expanduser()
        if not path.is_file():
            return False
        if not self.sha256:
            return True

        hasher = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                while chunk := f.read(65536):
                    hasher.update(chunk)
        except OSError:
            return False

        return hmac.compare_digest(hasher.hexdigest().lower(), self.sha256)

    def confine_local_path(self, media_root: Union[str, Path]) -> None:
        """Exige que ``local_path`` (resolviendo enlaces simbolicos) quede dentro de ``media_root``.

        Las rutas relativas se interpretan respecto a ``media_root``. Si la ruta es
        valida, ``local_path`` se reemplaza por su forma absoluta resuelta.

        Raises:
            ValueError: Si la ruta resuelta escapa de ``media_root``.
        """
        if not self.local_path:
            return
        root = Path(media_root).expanduser().resolve()
        candidate = Path(self.local_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(
                f"Ruta local no permitida en '{self.id}': '{self.local_path}' queda fuera de '{root}'."
            )
        self.local_path = str(resolved)

    def has_immediate_source(self) -> bool:
        """Indica si el elemento puede reproducirse sin esperar a una descarga (``remote_url`` o ``local_path``)."""
        return bool(self.remote_url or self.local_path)

    def is_downloaded(self) -> bool:
        """Indica si ya existe en disco el archivo preinstalado (``local_path``) o el descargado (``cache_path``)."""
        return any(
            path and Path(path).expanduser().is_file()
            for path in (self.local_path, self.cache_path)
        )

    def get_effective_uri(self) -> str:
        """Determina la URI efectiva a reproducir.

        Prioridad: archivo preinstalado en ``local_path``, archivo descargado en
        ``cache_path``, ``remote_url`` y, como ultimo recurso, la URI ``file://``
        de ``local_path`` aunque el archivo aun no exista.

        No verifica el SHA-256: esa comprobacion la realiza
        ``MediaDownloader.prepare`` antes de llamar a este metodo.

        Raises:
            ValueError: Si no hay ``local_path`` ni ``remote_url`` ni archivo en cache.
        """
        for candidate in (self.local_path, self.cache_path):
            if candidate:
                path = Path(candidate).expanduser()
                if path.is_file():
                    return path.resolve().as_uri()

        if self.remote_url:
            return self.remote_url

        if self.local_path:
            return Path(self.local_path).expanduser().resolve().as_uri()

        raise ValueError(
            f"El elemento multimedia '{self.id}' no dispone de local_path ni remote_url valido."
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaItem":
        """Construye un MediaItem desde un objeto JSON.

        Acepta los alias ``download_url`` (para ``s3_url``) y ``checksum`` (para ``sha256``).

        Raises:
            ValueError: Si ``data`` no es un objeto, si faltan ``id`` o ``title`` (o son
                ``null``), si ``sha256`` no es un hash valido, si ``metadata`` no es un
                objeto o si ``local_path`` contiene ``..``.
        """
        if not isinstance(data, dict):
            raise ValueError(f"Cada elemento multimedia debe ser un objeto JSON; se recibio {type(data).__name__}.")
        if data.get("id") is None or data.get("title") is None:
            raise ValueError("Cada elemento multimedia debe contener al menos 'id' y 'title'.")

        metadata = data.get("metadata")
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            remote_url=data.get("remote_url"),
            s3_url=data.get("s3_url") or data.get("download_url"),
            local_path=data.get("local_path"),
            sha256=data.get("sha256") or data.get("checksum"),
            duration=float(data["duration"]) if data.get("duration") is not None else None,
            metadata={} if metadata is None else metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el elemento con los nombres de campo canonicos (sin alias)."""
        return {
            "id": self.id,
            "title": self.title,
            "remote_url": self.remote_url,
            "s3_url": self.s3_url,
            "local_path": self.local_path,
            "sha256": self.sha256,
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
        """Construye una Playlist validando ``name`` y cada elemento de ``items``.

        ``loop`` solo admite ``true``/``false`` (o las cadenas ``"true"``/``"false"``).

        Raises:
            ValueError: Si falta ``name``, si ``loop`` no es booleano, si ``items`` no
                es una lista, si algun elemento es invalido o si hay ``id`` duplicados.
        """
        if "name" not in data:
            raise ValueError("La lista de reproduccion debe especificar un campo 'name'.")

        items_raw = data.get("items", [])
        if not isinstance(items_raw, list):
            raise ValueError("El campo 'items' debe ser una lista de elementos multimedia.")

        items = [MediaItem.from_dict(item) for item in items_raw]
        seen_ids = set()
        for item in items:
            if item.id in seen_ids:
                raise ValueError(f"El identificador '{item.id}' esta duplicado en la lista de reproduccion.")
            seen_ids.add(item.id)

        return cls(
            name=str(data["name"]),
            loop=parse_bool(data.get("loop", False), "loop"),
            items=items,
        )

    def confine_local_paths(self, media_root: Union[str, Path]) -> None:
        """Aplica ``MediaItem.confine_local_path`` a todos los elementos de la lista."""
        for item in self.items:
            item.confine_local_path(media_root)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la lista y sus elementos a un diccionario compatible con JSON."""
        return {
            "name": self.name,
            "loop": self.loop,
            "items": [item.to_dict() for item in self.items],
        }
