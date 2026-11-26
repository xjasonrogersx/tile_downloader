"""Add missing tile leaves to an existing downloaded area."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from download_tiles import (
    API_KEY,
    ARTIFACTS_DIR,
    BASE_DIR,
    HEIGHT_PROBE_OFFSETS,
    build_tile_url,
    fetch_center_height,
    fetch_tile_or_json,
    latlon_to_ecef,
    merge_gltf,
    traverse_for_leaf_glbs,
)


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


def offset_latlon(lat: float, lon: float, east_m: float, north_m: float) -> tuple[float, float]:
    lat_delta = north_m / 111320.0
    lon_delta = east_m / (111320.0 * math.cos(math.radians(lat)))
    return lat + lat_delta, lon + lon_delta


def sample_points(latitude: float, longitude: float, radius_meters: float, spacing_meters: float):
    """Return a square grid covering the selected area, including its boundary."""
    steps = max(1, math.ceil((radius_meters * 2) / spacing_meters))
    actual_spacing = (radius_meters * 2) / steps
    points = []
    for row in range(steps + 1):
        north = -radius_meters + row * actual_spacing
        for column in range(steps + 1):
            east = -radius_meters + column * actual_spacing
            points.append(offset_latlon(latitude, longitude, east, north))
    return points, actual_spacing


def scan_point(
    point: tuple[int, float, float],
    root_tileset: dict,
    root_url: str,
    terrain_height: float,
):
    index, latitude, longitude = point
    matches = []
    for height_offset in HEIGHT_PROBE_OFFSETS:
        target_ecef = latlon_to_ecef(latitude, longitude, terrain_height + height_offset)
        matches.extend(traverse_for_leaf_glbs(root_tileset["root"], target_ecef, API_KEY, root_url))
    return index, latitude, longitude, matches


def existing_hashes(parts_path: Path) -> set[bytes]:
    hashes = set()
    for path in parts_path.glob("*.glb"):
        hashes.add(hashlib.sha256(path.read_bytes()).digest())
    return hashes


def next_part_number(parts_path: Path) -> int:
    numbers = []
    for path in parts_path.glob("tile_*.glb"):
        try:
            numbers.append(int(path.stem.removeprefix("tile_")))
        except ValueError:
            continue
    return max(numbers, default=0) + 1


def main(area_dir: Path, spacing_meters: float, workers: int, dry_run: bool) -> int:
    metadata_path = area_dir / "metadata.json"
    parts_path = area_dir / "parts"
    if not metadata_path.exists() or not parts_path.is_dir():
        print(f"Error: {area_dir} is not a downloaded area with metadata.json and parts/.")
        return 2

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    required = ("latitude", "longitude", "radius_meters")
    if any(key not in metadata for key in required):
        print(f"Error: metadata.json is missing one of: {', '.join(required)}")
        return 2

    latitude = float(metadata["latitude"])
    longitude = float(metadata["longitude"])
    radius_meters = float(metadata["radius_meters"])
    points, actual_spacing = sample_points(latitude, longitude, radius_meters, spacing_meters)
    print(f"Area: {area_dir.name}")
    print(f"Coverage: {latitude:.6f}, {longitude:.6f}, radius {radius_meters:.1f} m")
    print(f"Gap scan: {len(points)} points at approximately {actual_spacing:.1f} m spacing and {len(HEIGHT_PROBE_OFFSETS)} height bands.")
    print(f"Existing tiles: {len(list(parts_path.glob('*.glb')))}")
    if dry_run:
        return 0

    started_at = time.perf_counter()
    root_url = build_tile_url("root.json", API_KEY)
    _, root_tileset = fetch_tile_or_json(root_url)
    terrain_height = fetch_center_height(latitude, longitude)
    print(f"Using terrain elevation {terrain_height:.1f} m.")

    indexed_points = [(index, point_lat, point_lon) for index, (point_lat, point_lon) in enumerate(points, start=1)]
    hashes = existing_hashes(parts_path)
    next_number = next_part_number(parts_path)
    new_tiles = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(scan_point, point, root_tileset, root_url, terrain_height): point[0]
            for point in indexed_points
        }
        for future in as_completed(futures):
            index, point_lat, point_lon, matches = future.result()
            completed += 1
            point_new_tiles = 0
            for glb_bytes in matches:
                content_hash = hashlib.sha256(glb_bytes).digest()
                if content_hash in hashes:
                    continue
                hashes.add(content_hash)
                part_path = parts_path / f"tile_{next_number:03d}.glb"
                part_path.write_bytes(glb_bytes)
                next_number += 1
                new_tiles += 1
                point_new_tiles += 1
            print(f"[{completed}/{len(points)}] Point {index} at {point_lat:.6f}, {point_lon:.6f}: {point_new_tiles} new tiles ({new_tiles} total new).")

    if not new_tiles:
        print("No additional GLBs found. Existing merged output was left unchanged.")
        return 0

    output_path = area_dir / "stitched.gltf"
    merge_gltf(parts_path, output_path)
    stitched_bin = output_path.with_suffix(".bin")
    if stitched_bin.exists():
        shutil.copy2(stitched_bin, area_dir / "merged.bin")
    shutil.copy2(output_path, area_dir / "merged.gltf")
    metadata["tile_count"] = len(list(parts_path.glob("*.glb")))
    metadata["last_gap_scan"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Added {new_tiles} new tiles; area now contains {metadata['tile_count']} unique GLBs.")
    print(f"Rebuilt merged model in {time.perf_counter() - started_at:.2f}s: {area_dir / 'merged.gltf'}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan an existing area for additional tile meshes and fill gaps.")
    parser.add_argument("area", help="Area directory name or path, for example area-20261125-223411-1dd082")
    parser.add_argument("--spacing", type=float, default=20.0, help="Approximate scan spacing in metres (default: 20).")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true", help="Print the scan size without making network requests.")
    args = parser.parse_args()
    if args.spacing <= 0 or args.workers < 1:
        parser.error("--spacing must be positive and --workers must be at least 1")

    area_arg = Path(args.area)
    area_dir = area_arg if area_arg.is_absolute() else ARTIFACTS_DIR / area_arg
    area_dir = area_dir.resolve()
    with (BASE_DIR / "log.txt").open("a", encoding="utf-8") as log_file:
        tee = OutputTee(sys.__stdout__, log_file)
        with redirect_stdout(tee), redirect_stderr(tee):
            raise SystemExit(main(area_dir, args.spacing, args.workers, args.dry_run))
