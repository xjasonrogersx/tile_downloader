"""Camera-sweep downloader for Google Photorealistic 3D Tiles."""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from download_tiles import (
    API_KEY,
    ARTIFACTS_DIR,
    BASE_DIR,
    CENTER_LAT,
    CENTER_LON,
    ELEVATION_API_URL,
    HEIGHT,
    OUTPUT_PATH,
    PARTS_PATH,
    build_tile_url,
    fetch_center_height,
    fetch_tile_or_json,
    latlon_to_ecef,
    merge_gltf,
    traverse_for_leaf_glbs,
)

CAMERA_EYE_OFFSET = 30.0
PAN_HEADINGS = (0.0, 90.0, 180.0, 270.0)
CAMERA_TILTS = (35.0, 55.0, 75.0)
CAMERA_RANGES = (20.0, 60.0, 120.0)
CAMERA_TARGET_HEIGHT_OFFSETS = (-50.0, 0.0, 50.0, 100.0)
GROUND_HEIGHT_OFFSETS = (-25.0, 0.0, 25.0, 50.0)


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


def camera_ray_targets(lat: float, lon: float, camera_height: float, terrain_height: float, footprint_half_m: float) -> list[tuple[float, float, float]]:
    targets = []
    # Explicitly sample the ground below the camera so narrow roads are not
    # lost while the wider pan rays find elevated building geometry.
    ground_offsets = (-footprint_half_m, 0.0, footprint_half_m)
    for north_m in ground_offsets:
        for east_m in ground_offsets:
            target_lat, target_lon = offset_latlon(lat, lon, east_m, north_m)
            for height_offset in GROUND_HEIGHT_OFFSETS:
                targets.append((target_lat, target_lon, terrain_height + height_offset))
    for heading in PAN_HEADINGS:
        heading_rad = math.radians(heading)
        for tilt, distance in zip(CAMERA_TILTS, CAMERA_RANGES):
            tilt_rad = math.radians(tilt)
            horizontal_distance = distance * math.cos(tilt_rad)
            east_m = horizontal_distance * math.sin(heading_rad)
            north_m = horizontal_distance * math.cos(heading_rad)
            target_lat, target_lon = offset_latlon(lat, lon, east_m, north_m)
            for height_offset in CAMERA_TARGET_HEIGHT_OFFSETS:
                target_height = terrain_height + height_offset
                targets.append((target_lat, target_lon, target_height))
    return targets


def camera_viewpoints(lat: float, lon: float, radius_m: float, steps: int, center_height: float):
    lat_offset = radius_m / 111000.0
    lon_offset = radius_m / (111000.0 * math.cos(math.radians(lat)))
    latitudes = [lat - lat_offset + (2 * lat_offset * i / max(1, steps - 1)) for i in range(steps)]
    longitudes = [lon - lon_offset + (2 * lon_offset * i / max(1, steps - 1)) for i in range(steps)]
    viewpoints = []
    for row, viewpoint_lat in enumerate(latitudes):
        for column, viewpoint_lon in enumerate(longitudes):
            camera_height = center_height + CAMERA_EYE_OFFSET
            viewpoints.append((row * steps + column + 1, viewpoint_lat, viewpoint_lon, camera_height))
    return viewpoints


def download_camera_viewpoint(viewpoint, root_tileset: dict, root_url: str, terrain_height: float, footprint_half_m: float):
    index, lat, lon, camera_height = viewpoint
    started_at = time.perf_counter()
    matches = []
    for target_lat, target_lon, target_height in camera_ray_targets(lat, lon, camera_height, terrain_height, footprint_half_m):
        target_ecef = latlon_to_ecef(target_lat, target_lon, target_height)
        matches.extend(traverse_for_leaf_glbs(root_tileset["root"], target_ecef, API_KEY, root_url))
    return index, lat, lon, matches, time.perf_counter() - started_at


def main(workers: int, latitude: float, longitude: float, radius_meters: float, grid_steps: int, artifact_dir: Path):
    global OUTPUT_PATH, PARTS_PATH
    started_at = time.perf_counter()
    output_path = artifact_dir / "stitched.gltf"
    parts_path = artifact_dir / "parts"
    parts_path.mkdir(parents=True, exist_ok=True)
    for filename in os.listdir(parts_path):
        if filename.startswith("tile_") and filename.endswith(".glb"):
            (parts_path / filename).unlink()
    for path in (output_path, output_path.with_suffix(".bin")):
        if path.exists():
            path.unlink()

    root_url = build_tile_url("root.json", API_KEY)
    _, root_tileset = fetch_tile_or_json(root_url)
    center_height = fetch_center_height(latitude, longitude)
    viewpoints = camera_viewpoints(latitude, longitude, radius_meters, grid_steps, center_height)
    footprint_half_m = radius_meters / max(1, grid_steps - 1)
    print(f"Using camera elevation {center_height + CAMERA_EYE_OFFSET:.1f} m ({center_height:.1f} m terrain + {CAMERA_EYE_OFFSET:.1f} m eye offset).")
    print(f"Camera sweep: {len(viewpoints)} viewpoints, ground footprint half-width {footprint_half_m:.1f} m, plus {len(PAN_HEADINGS)} pan headings, {len(CAMERA_TILTS)} downward rays, and {len(CAMERA_TARGET_HEIGHT_OFFSETS)} height bands per ray.")
    print(f"Using {workers} concurrent camera workers.")

    unique_glbs = set()
    downloaded_tiles = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = executor.map(lambda point: download_camera_viewpoint(point, root_tileset, root_url, center_height, footprint_half_m), viewpoints)
        for completed, (index, lat, lon, matches, elapsed) in enumerate(results, start=1):
            print(f"[{completed}/{len(viewpoints)}] Camera viewpoint {index} at Lat: {lat:.6f}, Lon: {lon:.6f} took {elapsed:.2f}s.")
            new_tiles = 0
            for glb_bytes in matches:
                content_hash = hash(glb_bytes)
                if content_hash in unique_glbs:
                    continue
                unique_glbs.add(content_hash)
                part_path = parts_path / f"tile_{downloaded_tiles + 1:03d}.glb"
                part_path.write_bytes(glb_bytes)
                downloaded_tiles += 1
                new_tiles += 1
            print(f"  -> {new_tiles} new unique tiles; {downloaded_tiles} total.")

    if not downloaded_tiles:
        print("Error: No GLB tiles were fetched.")
        return

    print(f"\nMerging {downloaded_tiles} unique tiles while preserving glTF node transforms...")
    merge_started_at = time.perf_counter()
    merge_gltf(parts_path, output_path)
    print(f"Successfully saved combined street GLTF to: {output_path}")
    print(f"Merge took {time.perf_counter() - merge_started_at:.2f}s; total camera download and merge took {time.perf_counter() - started_at:.2f}s.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download Google 3D Tiles using a bounded panning-camera sweep.")
    parser.add_argument("--latitude", type=float, default=CENTER_LAT)
    parser.add_argument("--longitude", type=float, default=CENTER_LON)
    parser.add_argument("--radius-meters", type=float, default=100.0)
    parser.add_argument("--grid-steps", type=int, default=3, help="Camera viewpoints per axis.")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACTS_DIR)
    args = parser.parse_args()
    if args.workers < 1 or args.grid_steps < 1:
        parser.error("--workers and --grid-steps must be at least 1")
    artifact_dir = args.artifact_dir.resolve()
    with (BASE_DIR / "log.txt").open("a", encoding="utf-8") as log_file:
        tee = OutputTee(sys.__stdout__, log_file)
        with redirect_stdout(tee), redirect_stderr(tee):
            main(args.workers, args.latitude, args.longitude, args.radius_meters, args.grid_steps, artifact_dir)
