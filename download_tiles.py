import io
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import struct
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import numpy as np
import requests
import trimesh
from PIL import Image
from trimesh.visual.texture import TextureVisuals
from tiles_common import (
    API_KEY,
    ARTIFACTS_DIR,
    BASE_DIR,
    CENTER_LAT,
    CENTER_LON,
    ELEVATION_API_URL,
    HEIGHT,
    HEIGHT_PROBE_OFFSETS,
    OutputTee,
    build_tile_url,
    fetch_center_height,
    fetch_tile_or_json,
    is_point_in_box,
    latlon_to_ecef,
    merge_gltf,
    store_tile,
    traverse_for_leaf_glb,
    traverse_for_leaf_glbs,
)

# Sampling Grid Parameters
GRID_STEPS = 7       # 7x7 grid gives enough samples to cover the requested area
RADIUS_METERS = 100   # Radius around center to fetch adjacent tiles


# ==========================================
# 1. Helpers & Grid Generators
# ==========================================
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


def download_sample_tiles(index: int, lat: float, lon: float, center_height: float, root_tileset: dict, root_url: str, workers: int) -> tuple[int, list[bytes], float]:
    started_at = time.perf_counter()
    sample_glbs = []
    for height_offset in HEIGHT_PROBE_OFFSETS:
        target_ecef = latlon_to_ecef(lat, lon, center_height + height_offset)
        sample_glbs.extend(traverse_for_leaf_glbs(root_tileset["root"], target_ecef, API_KEY, root_url))
    return index, sample_glbs, time.perf_counter() - started_at


# ==========================================
# 3. Execution & Mesh Stitching
# ==========================================
def main(workers: int):
    download_started_at = time.perf_counter()
    output_path = Path(ARTIFACTS_DIR) / "stitched.gltf"
    metadata_path = Path(ARTIFACTS_DIR) / "metadata.json"
    for path in (output_path, output_path.with_suffix(".bin")):
        if path.exists():
            path.unlink()

    root_url = build_tile_url("root.json", API_KEY)
    _, root_tileset = fetch_tile_or_json(root_url)

    grid_points = generate_grid_coords(CENTER_LAT, CENTER_LON, RADIUS_METERS, GRID_STEPS)
    center_height = fetch_center_height(CENTER_LAT, CENTER_LON)
    print(f"Using center elevation {center_height:.1f} m as the tile sampling height.")
    print(f"Sampling {len(grid_points)} grid points over a {RADIUS_METERS * 2}m x {RADIUS_METERS * 2}m area...")

    unique_hashes = set()
    tile_hashes = []

    print(f"Using {workers} concurrent download workers.")
    sample_args = [
        (idx, lat, lon, center_height, root_tileset, root_url, workers)
        for idx, (lat, lon) in enumerate(grid_points, start=1)
    ]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        sample_results = executor.map(lambda args: download_sample_tiles(*args), sample_args)
        for idx, sample_glbs, sample_seconds in sample_results:
            lat, lon = grid_points[idx - 1]
            print(f"[{idx}/{len(grid_points)}] Tile traversal for Lat: {lat:.6f}, Lon: {lon:.6f} took {sample_seconds:.2f}s.")

        if sample_glbs:
            new_tiles = 0
            # Hash GLB content to avoid adding duplicates
            for glb_bytes in sample_glbs:
                glb_hash = store_tile(glb_bytes)
                if glb_hash in unique_hashes:
                    continue
                unique_hashes.add(glb_hash)
                tile_hashes.append(glb_hash)
                new_tiles += 1
                print(f"  -> Stored unique tile mesh ({len(glb_bytes) // 1024} KB) in shared cache")
            if not new_tiles:
                print("  -> All matching tiles were already downloaded.")
        else:
            print("  -> No leaf tile found for this point.")

    if not tile_hashes:
        print("Error: No GLB tiles were fetched.")
        return

    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    metadata.update({"tile_hashes": tile_hashes, "tile_count": len(tile_hashes)})
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    print(f"\nMerging {len(tile_hashes)} unique tiles while preserving glTF node transforms...")
    merge_started_at = time.perf_counter()
    merge_gltf(Path(ARTIFACTS_DIR), output_path)
    merge_seconds = time.perf_counter() - merge_started_at
    total_seconds = time.perf_counter() - download_started_at
    print(f"Successfully saved combined street GLTF to: {output_path}")
    print(f"Merge took {merge_seconds:.2f}s; total download and merge took {total_seconds:.2f}s.")

    # Display preview window
    #merged_scene.show()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download and merge Google 3D tiles for a region.")
    parser.add_argument("--latitude", type=float, default=CENTER_LAT)
    parser.add_argument("--longitude", type=float, default=CENTER_LON)
    parser.add_argument("--radius-meters", type=float, default=RADIUS_METERS)
    parser.add_argument("--grid-steps", type=int, default=GRID_STEPS)
    parser.add_argument("--workers", type=int, default=8, help="Concurrent grid-point download workers.")
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACTS_DIR)
    args = parser.parse_args()
    CENTER_LAT = args.latitude
    CENTER_LON = args.longitude
    RADIUS_METERS = args.radius_meters
    GRID_STEPS = args.grid_steps
    ARTIFACTS_DIR = args.artifact_dir.resolve()
    OUTPUT_PATH = str(ARTIFACTS_DIR / "stitched.gltf")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    with (BASE_DIR / "log.txt").open("a", encoding="utf-8") as log_file:
        tee = OutputTee(sys.__stdout__, log_file)
        with redirect_stdout(tee), redirect_stderr(tee):
            main(args.workers)