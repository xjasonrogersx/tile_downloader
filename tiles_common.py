"""Shared Google 3D Tiles downloader configuration and traversal helpers."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import numpy as np
import requests

from merge_gltf import merge_gltf

BASE_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = BASE_DIR / "artifacts"
TOKEN_PATH = BASE_DIR / "token.txt"
API_KEY = TOKEN_PATH.read_text(encoding="utf-8").strip() if TOKEN_PATH.exists() else os.getenv("GOOGLE_3D_TILES_API_KEY", "")
OUTPUT_PATH = str(ARTIFACTS_DIR / "stitched.gltf")
PARTS_PATH = str(ARTIFACTS_DIR / "parts")
TARGET_GEOMETRIC_ERROR = 2.006368774808128
CENTER_LAT = 51.7451423
CENTER_LON = -0.466216
HEIGHT = 162.0
ELEVATION_API_URL = "https://api.open-meteo.com/v1/forecast"
HEIGHT_PROBE_OFFSETS = (-50.0, 0.0, 25.0, 50.0, 75.0, 100.0, 150.0)
FETCH_CACHE: dict[str, tuple[str, dict | bytes]] = {}
FETCH_CACHE_LOCK = threading.Lock()


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
        response = requests.get(
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
        print(f"Warning: could not fetch center elevation ({exc}); using fallback height {HEIGHT:.1f} m.")
        return HEIGHT


def is_point_in_box(point: tuple[float, float, float], box: list[float]) -> bool:
    """Check whether an ECEF point lies within a Cesium oriented bounding box."""
    center = box[:3]
    axes = np.asarray([box[3:6], box[6:9], box[9:12]], dtype=float).T
    if abs(np.linalg.det(axes)) < 1e-9:
        return False
    local_pos = np.linalg.solve(axes, np.asarray(point, dtype=float) - np.asarray(center, dtype=float))
    return bool(np.all(np.abs(local_pos) <= 1.0 + 1e-9))


def build_tile_url(uri: str, api_key: str, parent_url: str | None = None) -> str:
    if parent_url and not uri.startswith("http"):
        parsed_parent = urlparse(parent_url)
        base_path = parsed_parent.path.rsplit("/", 1)[0]
        if not uri.startswith("/"):
            uri = f"{base_path}/{uri}"
        base_url = f"{parsed_parent.scheme}://{parsed_parent.netloc}{uri}"
    elif not uri.startswith("http"):
        uri = uri.lstrip("/")
        base_url = f"https://tile.googleapis.com/v1/3dtiles/{uri}" if not uri.startswith("v1/") else f"https://tile.googleapis.com/{uri}"
    else:
        base_url = uri

    parsed = urlparse(base_url)
    query_params = parse_qs(parsed.query)
    if parent_url:
        parent_query = parse_qs(urlparse(parent_url).query)
        if "session" in parent_query and "session" not in query_params:
            query_params["session"] = parent_query["session"]
    query_params["key"] = [api_key]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query_params, doseq=True), parsed.fragment))


def fetch_tile_or_json(url: str) -> tuple[str, dict | bytes]:
    with FETCH_CACHE_LOCK:
        cached = FETCH_CACHE.get(url)
    if cached is not None:
        return cached

    last_error = None
    for attempt in range(4):
        try:
            response = requests.get(url, timeout=(10, 60))
            response.raise_for_status()
            content = response.content
            if "application/json" in response.headers.get("Content-Type", "").lower() or content.startswith(b"{"):
                result = ("json", response.json())
            else:
                result = ("glb", content)
            with FETCH_CACHE_LOCK:
                FETCH_CACHE[url] = result
            return result
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 3:
                break
            delay = 2 ** attempt
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


def traverse_for_leaf_glb(node: dict, target_ecef: tuple[float, float, float], api_key: str, parent_url: str, depth: int = 0) -> bytes | None:
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
        elif math.isclose(node.get("geometricError", float("inf")), TARGET_GEOMETRIC_ERROR, abs_tol=1e-6):
            return data
    return None


def traverse_for_leaf_glbs(node: dict, target_ecef: tuple[float, float, float], api_key: str, parent_url: str, depth: int = 0) -> list[bytes]:
    """Collect every matching street-level GLB in intersecting hierarchy branches."""
    bounding_box = node.get("boundingVolume", {}).get("box")
    if bounding_box and not is_point_in_box(target_ecef, bounding_box):
        return []

    matches = []
    for child in node.get("children", []):
        matches.extend(traverse_for_leaf_glbs(child, target_ecef, api_key, parent_url, depth + 1))
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
            matches.extend(traverse_for_leaf_glbs(data["root"], target_ecef, api_key, url, depth + 1))
        elif math.isclose(node.get("geometricError", float("inf")), TARGET_GEOMETRIC_ERROR, abs_tol=1e-6):
            matches.append(data)
    return matches
