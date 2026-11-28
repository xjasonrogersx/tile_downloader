"""Fetch street-level GLB tiles for one local coverage-grid cell."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from pathlib import Path

from tiles_common import (
    API_KEY,
    build_tile_url,
    fetch_center_height,
    fetch_tile_or_json,
    latlon_to_ecef,
    store_tile,
    traverse_for_leaf_glbs,
)

HEIGHT_OFFSETS = tuple(float(value) for value in (
    -20, -10, 0, 10, 20, 25, 30, 35,
    *range(38, 73, 2),
    80, 90, 100, 130, 160,
))


def offset_latlon(lat: float, lon: float, east_m: float, north_m: float) -> tuple[float, float]:
    lat_delta = north_m / 111320.0
    lon_delta = east_m / (111320.0 * max(0.01, math.cos(math.radians(lat))))
    return lat + lat_delta, lon + lon_delta


def cell_region_axes(latitude: float, longitude: float, center_height: float, size_m: float) -> list[tuple[float, float, float]]:
    lat_radians = math.radians(latitude)
    lon_radians = math.radians(longitude)
    east = (-math.sin(lon_radians), math.cos(lon_radians), 0.0)
    north = (
        -math.sin(lat_radians) * math.cos(lon_radians),
        -math.sin(lat_radians) * math.sin(lon_radians),
        math.cos(lat_radians),
    )
    up = (
        math.cos(lat_radians) * math.cos(lon_radians),
        math.cos(lat_radians) * math.sin(lon_radians),
        math.sin(lat_radians),
    )
    region_center = latlon_to_ecef(latitude, longitude, center_height + 70.0)
    return [
        region_center,
        tuple(component * (size_m / 2.0) for component in east),
        tuple(component * (size_m / 2.0) for component in north),
        tuple(component * 90.0 for component in up),
    ]


def fetch_cell(latitude: float, longitude: float, size_m: float, samples_per_axis: int) -> dict:
    root_url = build_tile_url("root.json", API_KEY)
    _, root_tileset = fetch_tile_or_json(root_url)
    center_height = fetch_center_height(latitude, longitude)
    probe_size_m = size_m * 2.0
    region_axes = cell_region_axes(latitude, longitude, center_height, probe_size_m)
    half_size = probe_size_m / 2.0
    offsets = [0.0] if samples_per_axis <= 1 else [
        -half_size + (size_m * index / (samples_per_axis - 1))
        for index in range(samples_per_axis)
    ]
    hashes: list[str] = []
    seen: set[str] = set()
    raw_hits = 0
    errors = 0
    hit_offsets: set[float] = set()
    total_samples = len(offsets) * len(offsets) * len(HEIGHT_OFFSETS)
    completed_samples = 0
    started_at = time.perf_counter()
    for north_m in offsets:
        for east_m in offsets:
            for height_offset in HEIGHT_OFFSETS:
                try:
                    sample_lat, sample_lon = offset_latlon(latitude, longitude, east_m, north_m)
                    target = latlon_to_ecef(sample_lat, sample_lon, center_height + height_offset)
                    matches = traverse_for_leaf_glbs(
                        root_tileset["root"], target, API_KEY, root_url, region_axes=region_axes
                    )
                    if matches:
                        hit_offsets.add(height_offset)
                    raw_hits += len(matches)
                    for glb_bytes in matches:
                        content_hash = hashlib.sha256(glb_bytes).hexdigest()
                        if content_hash not in seen:
                            seen.add(content_hash)
                            hashes.append(store_tile(glb_bytes))
                except Exception:
                    errors += 1
                completed_samples += 1
                if completed_samples == 1 or completed_samples % max(1, total_samples // 10) == 0 or completed_samples == total_samples:
                    print("PROGRESS=" + json.dumps({"completed": completed_samples, "total": total_samples, "raw_hits": raw_hits, "unique_tiles": len(hashes), "hit_offsets": sorted(hit_offsets), "errors": errors, "center_elevation_m": center_height}), flush=True)
    return {"tile_hashes": hashes, "center_elevation_m": center_height, "probe_size_m": probe_size_m, "samples": total_samples, "raw_hits": raw_hits, "errors": errors, "hit_offsets": sorted(hit_offsets), "elapsed_seconds": round(time.perf_counter() - started_at, 2)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch tiles for one coverage-grid cell.")
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    parser.add_argument("--size-meters", type=float, required=True)
    parser.add_argument("--samples-per-axis", type=int, default=7)
    args = parser.parse_args()
    result = fetch_cell(args.latitude, args.longitude, args.size_meters, args.samples_per_axis)
    print("RESULT=" + json.dumps(result))
