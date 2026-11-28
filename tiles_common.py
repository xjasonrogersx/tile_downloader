"""Shared Google 3D Tiles downloader configuration and traversal helpers."""

from __future__ import annotations

import json
import hashlib
import math
import os
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import numpy as np
import requests
from requests.adapters import HTTPAdapter

BASE_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = BASE_DIR / "artifacts"
GLOBAL_TILES_DIR = ARTIFACTS_DIR / "tiles"
TILE_URL_INDEX_PATH = GLOBAL_TILES_DIR / "url_index.json"
TOKEN_PATH = BASE_DIR / "token.txt"
API_KEY = (
    TOKEN_PATH.read_text(encoding="utf-8").strip()
    if TOKEN_PATH.exists()
    else os.getenv("GOOGLE_3D_TILES_API_KEY", "")
)
OUTPUT_PATH = str(ARTIFACTS_DIR / "stitched.gltf")
TARGET_GEOMETRIC_ERROR = 2.006368774808128
CENTER_LAT = 51.7451423
CENTER_LON = -0.466216
HEIGHT = 162.0
ELEVATION_API_URL = "https://api.open-meteo.com/v1/forecast"
HEIGHT_PROBE_OFFSETS = (-50.0, 0.0, 25.0, 50.0, 75.0, 100.0, 150.0)

FETCH_CACHE: dict[str, tuple[str, dict | bytes]] = {}
FETCH_CACHE_LOCK = threading.Lock()
_STATS_LOCK = threading.Lock()
_FETCH_STATS = {
    "network_requests": 0,
    "cache_hits": 0,
    "json_fetches": 0,
    "glb_fetches": 0,
}

_SESSION_LOCK = threading.Lock()
_HTTP_SESSION: requests.Session | None = None
_TILE_INDEX_LOCK = threading.Lock()
_TILE_URL_INDEX: dict[str, str] | None = None


def merge_gltf(*args, **kwargs):
    from merge_gltf import merge_gltf as merge_impl

    return merge_impl(*args, **kwargs)


def tile_hash(glb_bytes: bytes) -> str:
    return hashlib.sha256(glb_bytes).hexdigest()


def tile_cache_path(content_hash: str, cache_dir: Path = GLOBAL_TILES_DIR) -> Path:
    if len(content_hash) != 64 or any(character not in "0123456789abcdef" for character in content_hash):
        raise ValueError(f"Invalid tile hash: {content_hash}")
    return cache_dir / f"{content_hash}.glb"


def store_tile(glb_bytes: bytes, cache_dir: Path = GLOBAL_TILES_DIR) -> str:
    """Store a GLB once in the shared cache and return its SHA-256 hash."""
    content_hash = tile_hash(glb_bytes)
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = tile_cache_path(content_hash, cache_dir)
    if destination.exists():
        return content_hash

    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_bytes(glb_bytes)
        os.replace(temporary, destination)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
    return content_hash


def _tile_url_key(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query.pop("key", None)
    query.pop("session", None)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query, doseq=True), "")
    )


def _load_tile_url_index() -> dict[str, str]:
    global _TILE_URL_INDEX
    with _TILE_INDEX_LOCK:
        if _TILE_URL_INDEX is None:
            if TILE_URL_INDEX_PATH.exists():
                try:
                    loaded = json.loads(TILE_URL_INDEX_PATH.read_text(encoding="utf-8"))
                    _TILE_URL_INDEX = loaded if isinstance(loaded, dict) else {}
                except (OSError, json.JSONDecodeError):
                    _TILE_URL_INDEX = {}
            else:
                _TILE_URL_INDEX = {}
        return _TILE_URL_INDEX


def cached_tile_for_url(url: str) -> bytes | None:
    content_hash = _load_tile_url_index().get(_tile_url_key(url))
    if not content_hash:
        return None
    path = tile_cache_path(content_hash)
    try:
        return path.read_bytes()
    except OSError:
        return None


def remember_tile_url(url: str, content_hash: str) -> None:
    index = _load_tile_url_index()
    with _TILE_INDEX_LOCK:
        index[_tile_url_key(url)] = content_hash
        TILE_URL_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = TILE_URL_INDEX_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, TILE_URL_INDEX_PATH)


def tile_paths_from_metadata(metadata_path: Path, cache_dir: Path = GLOBAL_TILES_DIR) -> list[Path]:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    hashes = metadata.get("tile_hashes")
    if not isinstance(hashes, list) or not hashes:
        raise ValueError(f"{metadata_path}: metadata.json has no tile_hashes")
    paths = [tile_cache_path(str(content_hash), cache_dir) for content_hash in hashes]
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing cached tile: {missing[0]}")
    return paths


def _get_session(pool_size: int = 32) -> requests.Session:
    global _HTTP_SESSION
    with _SESSION_LOCK:
        if _HTTP_SESSION is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            _HTTP_SESSION = session
        return _HTTP_SESSION


def reset_fetch_stats() -> None:
    with _STATS_LOCK:
        for key in _FETCH_STATS:
            _FETCH_STATS[key] = 0


def get_fetch_stats() -> dict[str, int]:
    with _STATS_LOCK:
        return dict(_FETCH_STATS)


class OutputTee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def latlon_to_ecef(lat: float, lon: float, height: float) -> tuple[float, float, float]:
    """Convert WGS84 latitude/longitude/height to ECEF coordinates."""
    a = 6378137.0
    e_sq = 6.69437999014e-3
    rad_lat = math.radians(lat)
    rad_lon = math.radians(lon)
    n = a / math.sqrt(1 - e_sq * (math.sin(rad_lat) ** 2))
    x = (n + height) * math.cos(rad_lat) * math.cos(rad_lon)
    y = (n + height) * math.cos(rad_lat) * math.sin(rad_lon)
    z = (n * (1 - e_sq) + height) * math.sin(rad_lat)
    return x, y, z


def fetch_center_height(lat: float, lon: float) -> float:
    """Fetch center elevation, falling back when the service is unavailable."""
    try:
        response = _get_session().get(
            ELEVATION_API_URL,
            params={"latitude": lat, "longitude": lon, "current": "temperature_2m"},
            timeout=10,
        )
        response.raise_for_status()
        elevation = response.json().get("elevation")
        if elevation is None:
            raise ValueError("Elevation was missing from the API response")
        return float(elevation)
    except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(
            f"Warning: could not fetch center elevation ({exc}); using fallback height {HEIGHT:.1f} m."
        )
        return HEIGHT


def is_point_in_box(point: tuple[float, float, float], box: list[float]) -> bool:
    """Check whether an ECEF point lies within a Cesium oriented bounding box."""
    center = box[:3]
    axes = np.asarray([box[3:6], box[6:9], box[9:12]], dtype=float).T
    if abs(np.linalg.det(axes)) < 1e-9:
        return False
    local_pos = np.linalg.solve(
        axes, np.asarray(point, dtype=float) - np.asarray(center, dtype=float)
    )
    return bool(np.all(np.abs(local_pos) <= 1.0 + 1e-9))


def is_box_intersecting_obb(box: list[float], other_center: tuple[float, float, float], other_axes: list[tuple[float, float, float]]) -> bool:
    """Check whether a Cesium oriented box intersects another oriented box."""
    box_center = np.asarray(box[:3], dtype=float)
    box_vectors = [np.asarray(box[index:index + 3], dtype=float) for index in (3, 6, 9)]
    other_center_array = np.asarray(other_center, dtype=float)
    other_vectors = [np.asarray(axis, dtype=float) for axis in other_axes]
    box_extents = [float(np.linalg.norm(vector)) for vector in box_vectors]
    other_extents = [float(np.linalg.norm(vector)) for vector in other_vectors]
    box_units = [vector / extent for vector, extent in zip(box_vectors, box_extents) if extent > 1e-9]
    other_units = [vector / extent for vector, extent in zip(other_vectors, other_extents) if extent > 1e-9]
    axes = box_units + other_units
    axes.extend(
        np.cross(box_axis, other_axis)
        for box_axis in box_units
        for other_axis in other_units
    )
    center_delta = other_center_array - box_center
    for axis in axes:
        axis_length = float(np.linalg.norm(axis))
        if axis_length <= 1e-9:
            continue
        unit_axis = axis / axis_length
        box_radius = sum(extent * abs(float(np.dot(unit_axis, unit))) for extent, unit in zip(box_extents, box_units))
        other_radius = sum(extent * abs(float(np.dot(unit_axis, unit))) for extent, unit in zip(other_extents, other_units))
        if abs(float(np.dot(center_delta, unit_axis))) > box_radius + other_radius + 1e-6:
            return False
    return True


def build_tile_url(uri: str, api_key: str, parent_url: str | None = None) -> str:
    if parent_url and not uri.startswith("http"):
        parsed_parent = urlparse(parent_url)
        base_path = parsed_parent.path.rsplit("/", 1)[0]
        if not uri.startswith("/"):
            uri = f"{base_path}/{uri}"
        base_url = f"{parsed_parent.scheme}://{parsed_parent.netloc}{uri}"
    elif not uri.startswith("http"):
        uri = uri.lstrip("/")
        base_url = (
            f"https://tile.googleapis.com/v1/3dtiles/{uri}"
            if not uri.startswith("v1/")
            else f"https://tile.googleapis.com/{uri}"
        )
    else:
        base_url = uri

    parsed = urlparse(base_url)
    query_params = parse_qs(parsed.query)
    if parent_url:
        parent_query = parse_qs(urlparse(parent_url).query)
        if "session" in parent_query and "session" not in query_params:
            query_params["session"] = parent_query["session"]
    query_params["key"] = [api_key]
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query_params, doseq=True),
            parsed.fragment,
        )
    )


def fetch_tile_or_json(url: str) -> tuple[str, dict | bytes]:
    with FETCH_CACHE_LOCK:
        cached = FETCH_CACHE.get(url)
    if cached is not None:
        with _STATS_LOCK:
            _FETCH_STATS["cache_hits"] += 1
        return cached

    persistent_tile = cached_tile_for_url(url)
    if persistent_tile is not None:
        result = ("glb", persistent_tile)
        with FETCH_CACHE_LOCK:
            FETCH_CACHE[url] = result
        with _STATS_LOCK:
            _FETCH_STATS["cache_hits"] += 1
        return result

    session = _get_session()
    last_error = None
    for attempt in range(4):
        try:
            with _STATS_LOCK:
                _FETCH_STATS["network_requests"] += 1
            response = session.get(url, timeout=(10, 60))
            response.raise_for_status()
            content = response.content
            is_json = (
                "application/json" in response.headers.get("Content-Type", "").lower()
                or content.startswith(b"{")
            )
            if is_json:
                result = ("json", response.json())
                with _STATS_LOCK:
                    _FETCH_STATS["json_fetches"] += 1
            else:
                result = ("glb", content)
                content_hash = store_tile(content)
                remember_tile_url(url, content_hash)
                with _STATS_LOCK:
                    _FETCH_STATS["glb_fetches"] += 1
            with FETCH_CACHE_LOCK:
                FETCH_CACHE[url] = result
            return result
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 3:
                break
            delay = 2**attempt
            print(f"Warning: tile request failed ({exc}); retrying in {delay}s.")
            time.sleep(delay)
    raise requests.RequestException(f"Tile request failed after 4 attempts: {last_error}")


def _contents(node: dict) -> list[dict]:
    contents = []
    if "content" in node:
        contents.append(node["content"])
    if "contents" in node:
        contents.extend(node["contents"])
    return contents


def traverse_for_leaf_glb(
    node: dict,
    target_ecef: tuple[float, float, float],
    api_key: str,
    parent_url: str,
    depth: int = 0,
) -> bytes | None:
    bounding_box = node.get("boundingVolume", {}).get("box")
    if bounding_box and not is_point_in_box(target_ecef, bounding_box):
        return None

    for child in node.get("children", []):
        result = traverse_for_leaf_glb(child, target_ecef, api_key, parent_url, depth + 1)
        if result:
            return result

    for item in _contents(node):
        uri = item.get("uri") or item.get("url")
        if not uri:
            continue
        url = build_tile_url(uri, api_key, parent_url)
        try:
            data_type, data = fetch_tile_or_json(url)
        except requests.RequestException as exc:
            print(f"Warning: skipping unavailable tile content: {exc}")
            continue
        if data_type == "json":
            result = traverse_for_leaf_glb(data["root"], target_ecef, api_key, url, depth + 1)
            if result:
                return result
        elif math.isclose(
            node.get("geometricError", float("inf")), TARGET_GEOMETRIC_ERROR, abs_tol=1e-6
        ):
            return data
    return None


def traverse_for_leaf_glbs(
    node: dict,
    target_ecef: tuple[float, float, float],
    api_key: str,
    parent_url: str,
    depth: int = 0,
    region_axes: list[tuple[float, float, float]] | None = None,
) -> list[bytes]:
    """Collect every matching street-level GLB in intersecting hierarchy branches."""
    bounding_box = node.get("boundingVolume", {}).get("box")
    if bounding_box and not is_point_in_box(target_ecef, bounding_box) and (
        region_axes is None or not is_box_intersecting_obb(bounding_box, region_axes[0], region_axes[1:])
    ):
        return []

    matches = []
    for child in node.get("children", []):
        matches.extend(
            traverse_for_leaf_glbs(child, target_ecef, api_key, parent_url, depth + 1)
        )
    for item in _contents(node):
        uri = item.get("uri") or item.get("url")
        if not uri:
            continue
        url = build_tile_url(uri, api_key, parent_url)
        try:
            data_type, data = fetch_tile_or_json(url)
        except requests.RequestException as exc:
            print(f"Warning: skipping unavailable tile content: {exc}")
            continue
        if data_type == "json":
            matches.extend(
                traverse_for_leaf_glbs(
                    data["root"], target_ecef, api_key, url, depth + 1, region_axes
                )
            )
        elif math.isclose(
            node.get("geometricError", float("inf")), TARGET_GEOMETRIC_ERROR, abs_tol=1e-6
        ):
            matches.append(data)
    return matches