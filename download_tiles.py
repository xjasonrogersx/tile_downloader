import io
import argparse
import json
import math
import os
import struct
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
import numpy as np
import requests
import trimesh
from PIL import Image
from trimesh.visual.texture import TextureVisuals
from merge_gltf import merge_gltf

# ==========================================
# Configuration
# ==========================================
BASE_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = BASE_DIR / "artifacts"
TOKEN_PATH = BASE_DIR / "token.txt"
API_KEY = TOKEN_PATH.read_text(encoding="utf-8").strip() if TOKEN_PATH.exists() else os.getenv("GOOGLE_3D_TILES_API_KEY", "")
OUTPUT_PATH = str(ARTIFACTS_DIR / "stitched.gltf")
PARTS_PATH = str(ARTIFACTS_DIR / "parts")
TARGET_GEOMETRIC_ERROR = 2.006368774808128

# Center location (Hemel Hempstead center example)
CENTER_LAT = 51.7451423
CENTER_LON = -0.466216
HEIGHT = 162.0

# Sampling Grid Parameters
GRID_STEPS = 7       # 7x7 grid gives enough samples to cover the requested area
RADIUS_METERS = 100   # Radius around center to fetch adjacent tiles


# ==========================================
# 1. Helpers & Grid Generators
# ==========================================
def latlon_to_ecef(lat: float, lon: float, height: float) -> tuple[float, float, float]:
    """Convert WGS84 latitude/longitude/height to ECEF coordinates."""
    a = 6378137.0
    e_sq = 6.69437999014e-3
    rad_lat = math.radians(lat)
    rad_lon = math.radians(lon)

    N = a / math.sqrt(1 - e_sq * (math.sin(rad_lat) ** 2))
    x = (N + height) * math.cos(rad_lat) * math.cos(rad_lon)
    y = (N + height) * math.cos(rad_lat) * math.sin(rad_lon)
    z = (N * (1 - e_sq) + height) * math.sin(rad_lat)
    return x, y, z


def generate_grid_coords(lat: float, lon: float, radius_m: float, steps: int):
    """Generate a grid of lat/lon points around a center location."""
    # Rough degree offsets (1 deg lat ~= 111,000m, 1 deg lon ~= 111,000m * cos(lat))
    lat_offset = radius_m / 111000.0
    lon_offset = radius_m / (111000.0 * math.cos(math.radians(lat)))

    lats = np.linspace(lat - lat_offset, lat + lat_offset, steps)
    lons = np.linspace(lon - lon_offset, lon + lon_offset, steps)

    coords = []
    for l_val in lats:
        for r_val in lons:
            coords.append((float(l_val), float(r_val)))
    return coords


def is_point_in_box(point: tuple[float, float, float], box: list[float]) -> bool:
    """Check if an ECEF point lies within an OBB [cx, cy, cz, ux, uy, uz, vx, vy, vz, wx, wy, wz]."""
    center = box[:3]
    u, v, w = box[3:6], box[6:9], box[9:12]
    axes = np.asarray([u, v, w], dtype=float).T

    if abs(np.linalg.det(axes)) < 1e-9:
        return False

    local_pos = np.linalg.solve(axes, np.asarray(point, dtype=float) - np.asarray(center, dtype=float))
    return bool(np.all(np.abs(local_pos) <= 1.0 + 1e-9))


# ==========================================
# 2. Network Requests & Hierarchy Traversal
# ==========================================
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
    resp = requests.get(url)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "").lower()
    content = resp.content

    if "application/json" in content_type or content.startswith(b"{"):
        return "json", resp.json()
    return "glb", content


def load_textured_scene(glb_bytes: bytes) -> trimesh.Scene:
    """Load a GLB and restore its embedded image for trimesh export."""
    scene = trimesh.load(trimesh.util.wrap_as_stream(glb_bytes), file_type="glb")
    if len(glb_bytes) < 20:
        return scene

    json_length, json_type = struct.unpack_from("<II", glb_bytes, 12)
    if json_type != 0x4E4F534A:
        return scene
    gltf = json.loads(glb_bytes[20:20 + json_length].decode("utf-8"))
    images = gltf.get("images", [])
    if not images or "bufferView" not in images[0]:
        return scene

    bin_offset = 20 + json_length
    bin_length, bin_type = struct.unpack_from("<II", glb_bytes, bin_offset)
    if bin_type != 0x004E4942:
        return scene
    binary = glb_bytes[bin_offset + 8:bin_offset + 8 + bin_length]
    image_view = gltf["bufferViews"][images[0]["bufferView"]]
    image_start = image_view.get("byteOffset", 0)
    image_end = image_start + image_view["byteLength"]
    image = Image.open(io.BytesIO(binary[image_start:image_end])).convert("RGBA")

    for geometry in scene.geometry.values():
        uv = getattr(geometry.visual, "uv", None)
        if uv is not None:
            geometry.visual = TextureVisuals(uv=uv, image=image)
    return scene


def traverse_for_leaf_glb(node: dict, target_ecef: tuple[float, float, float], api_key: str, parent_url: str, depth: int = 0) -> bytes | None:
    children = node.get("children", [])
    geometric_error = node.get("geometricError", float("inf"))
    bounding_box = node.get("boundingVolume", {}).get("box")

    if bounding_box and not is_point_in_box(target_ecef, bounding_box):
        return None

    contents = []
    if "content" in node:
        contents.append(node["content"])
    if "contents" in node:
        contents.extend(node["contents"])

    # Traverse deeper first for street-level resolution
    for child in children:
        result = traverse_for_leaf_glb(child, target_ecef, api_key, parent_url, depth + 1)
        if result:
            return result

    # Evaluate current node content
    for item in contents:
        uri = item.get("uri") or item.get("url")
        if not uri:
            continue

        url = build_tile_url(uri, api_key, parent_url)
        data_type, data = fetch_tile_or_json(url)

        if data_type == "json":
            result = traverse_for_leaf_glb(data["root"], target_ecef, api_key, url, depth + 1)
            if result:
                return result
        elif data_type == "glb":
            if math.isclose(geometric_error, TARGET_GEOMETRIC_ERROR, abs_tol=1e-6):
                return data

    return None


# ==========================================
# 3. Execution & Mesh Stitching
# ==========================================
def main():
    os.makedirs(PARTS_PATH, exist_ok=True)
    for filename in os.listdir(PARTS_PATH):
        if filename.startswith("tile_") and filename.endswith(".glb"):
            os.remove(os.path.join(PARTS_PATH, filename))
    for output_path in (OUTPUT_PATH, str(Path(OUTPUT_PATH).with_suffix(".bin"))):
        if os.path.exists(output_path):
            os.remove(output_path)

    root_url = build_tile_url("root.json", API_KEY)
    _, root_tileset = fetch_tile_or_json(root_url)

    grid_points = generate_grid_coords(CENTER_LAT, CENTER_LON, RADIUS_METERS, GRID_STEPS)
    print(f"Sampling {len(grid_points)} grid points over a {RADIUS_METERS * 2}m x {RADIUS_METERS * 2}m area...")

    unique_glbs = set()
    downloaded_tiles = 0

    for idx, (lat, lon) in enumerate(grid_points, start=1):
        target_ecef = latlon_to_ecef(lat, lon, HEIGHT)
        print(f"[{idx}/{len(grid_points)}] Traversing tile for Lat: {lat:.6f}, Lon: {lon:.6f}...")

        glb_bytes = traverse_for_leaf_glb(root_tileset["root"], target_ecef, API_KEY, root_url)

        if glb_bytes:
            # Hash GLB content to avoid adding duplicates
            glb_hash = hash(glb_bytes)
            if glb_hash not in unique_glbs:
                unique_glbs.add(glb_hash)
                os.makedirs(PARTS_PATH, exist_ok=True)
                part_path = os.path.join(PARTS_PATH, f"tile_{downloaded_tiles + 1:03d}.glb")
                with open(part_path, "wb") as part_file:
                    part_file.write(glb_bytes)
                downloaded_tiles += 1
                print(f"  -> Downloaded new unique tile mesh ({len(glb_bytes) // 1024} KB) to {part_path}")
            else:
                print("  -> Duplicate tile returned; skipping.")
        else:
            print("  -> No leaf tile found for this point.")

    if not downloaded_tiles:
        print("Error: No GLB tiles were fetched.")
        return

    print(f"\nMerging {downloaded_tiles} unique tiles while preserving glTF node transforms...")
    merge_gltf(Path(PARTS_PATH), Path(OUTPUT_PATH))
    print(f"Successfully saved combined street GLTF to: {OUTPUT_PATH}")

    # Display preview window
    #merged_scene.show()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download and merge Google 3D tiles for a region.")
    parser.add_argument("--latitude", type=float, default=CENTER_LAT)
    parser.add_argument("--longitude", type=float, default=CENTER_LON)
    parser.add_argument("--radius-meters", type=float, default=RADIUS_METERS)
    parser.add_argument("--grid-steps", type=int, default=GRID_STEPS)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACTS_DIR)
    args = parser.parse_args()
    CENTER_LAT = args.latitude
    CENTER_LON = args.longitude
    RADIUS_METERS = args.radius_meters
    GRID_STEPS = args.grid_steps
    ARTIFACTS_DIR = args.artifact_dir.resolve()
    OUTPUT_PATH = str(ARTIFACTS_DIR / "stitched.gltf")
    PARTS_PATH = str(ARTIFACTS_DIR / "parts")
    main()