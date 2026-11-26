"""Camera-sweep downloader for Google Photorealistic 3D Tiles."""

from __future__ import annotations

import argparse
import hashlib
import logging
import math
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tiles_common import (
    API_KEY,
    ARTIFACTS_DIR,
    BASE_DIR,
    CENTER_LAT,
    CENTER_LON,
    OutputTee,
    build_tile_url,
    fetch_center_height,
    fetch_tile_or_json,
    get_fetch_stats,
    latlon_to_ecef,
    merge_gltf,
    reset_fetch_stats,
    traverse_for_leaf_glbs,
)

CAMERA_EYE_OFFSET = 30.0
# Concentrated around observed +35m..+55m hit band with a small buffer on both sides
SURFACE_HEIGHT_OFFSETS = (30.0, 38.0, 44.0, 48.0, 52.0, 56.0, 62.0, 70.0)


def setup_debug_logger(debug_log_path: Path) -> logging.Logger:
    logger = logging.getLogger("tile_sweep_debug")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    handler = logging.FileHandler(debug_log_path, mode="w", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def log(msg: str) -> None:
    print(msg, flush=True)


def offset_latlon(lat: float, lon: float, east_m: float, north_m: float) -> tuple[float, float]:
    lat_delta = north_m / 111320.0
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    lon_delta = east_m / (111320.0 * cos_lat)
    return lat + lat_delta, lon + lon_delta


def linear_axis(span_half_m: float, count: int) -> list[float]:
    if count <= 1:
        return [0.0]
    return [-span_half_m + (2.0 * span_half_m * i / (count - 1)) for i in range(count)]


def surface_targets(
    lat: float,
    lon: float,
    terrain_height: float,
    span_half_m: float,
    samples_per_axis: int = 7,
) -> list[tuple[float, float, float, float]]:
    """Return deterministic (lat, lon, height, h_offset) samples over [-span_half_m, +span_half_m]."""
    offsets = linear_axis(span_half_m, samples_per_axis)
    targets: list[tuple[float, float, float, float]] = []
    for north_m in offsets:
        for east_m in offsets:
            t_lat, t_lon = offset_latlon(lat, lon, east_m, north_m)
            for h_off in SURFACE_HEIGHT_OFFSETS:
                targets.append((t_lat, t_lon, terrain_height + h_off, h_off))
    return targets


def camera_viewpoints(
    lat: float, lon: float, radius_m: float, steps: int, center_height: float
) -> list[tuple[int, float, float, float]]:
    coords = linear_axis(radius_m, steps)
    camera_height = center_height + CAMERA_EYE_OFFSET
    viewpoints = []
    idx = 1
    for north_m in coords:
        for east_m in coords:
            vp_lat, vp_lon = offset_latlon(lat, lon, east_m, north_m)
            viewpoints.append((idx, vp_lat, vp_lon, camera_height))
            idx += 1
    return viewpoints


def download_camera_viewpoint(
    viewpoint,
    root_tileset: dict,
    root_url: str,
    terrain_height: float,
    span_half_m: float,
    samples_per_axis: int,
    dbg: logging.Logger,
):
    index, lat, lon, _ = viewpoint
    started_at = time.perf_counter()
    unique_matches: dict[bytes, bytes] = {}
    hit_offsets: set[float] = set()
    raw_match_count = 0
    error_count = 0

    targets = surface_targets(lat, lon, terrain_height, span_half_m, samples_per_axis)
    for sample_idx, (t_lat, t_lon, t_h, h_off) in enumerate(targets):
        try:
            target_ecef = latlon_to_ecef(t_lat, t_lon, t_h)
            leaf_glbs = traverse_for_leaf_glbs(
                root_tileset["root"], target_ecef, API_KEY, root_url
            )
        except Exception as exc:
            error_count += 1
            dbg.error(
                "VP %d sample=%d target=(%.6f, %.6f, %.1f) error: %s\n%s",
                index,
                sample_idx,
                t_lat,
                t_lon,
                t_h,
                exc,
                traceback.format_exc(),
            )
            continue

        if leaf_glbs:
            hit_offsets.add(h_off)

        for glb_bytes in leaf_glbs:
            raw_match_count += 1
            digest = hashlib.sha256(glb_bytes).digest()
            if digest not in unique_matches:
                unique_matches[digest] = glb_bytes

    elapsed = time.perf_counter() - started_at
    dbg.info(
        "VP %d done in %.2fs: targets=%d raw_hits=%d unique_in_vp=%d hit_h_offs=%s errors=%d",
        index,
        elapsed,
        len(targets),
        raw_match_count,
        len(unique_matches),
        sorted(hit_offsets),
        error_count,
    )
    return index, lat, lon, unique_matches, len(targets), raw_match_count, error_count, elapsed


def main(
    workers: int,
    latitude: float,
    longitude: float,
    radius_meters: float,
    grid_steps: int,
    samples_per_axis: int,
    artifact_dir: Path,
):
    reset_fetch_stats()
    started_at = time.perf_counter()
    output_path = artifact_dir / "stitched.gltf"
    parts_path = artifact_dir / "parts"
    debug_log_path = artifact_dir / "debug.log"
    parts_path.mkdir(parents=True, exist_ok=True)
    dbg = setup_debug_logger(debug_log_path)

    for filename in os.listdir(parts_path):
        if filename.startswith("tile_") and filename.endswith(".glb"):
            (parts_path / filename).unlink()
    for path in (output_path, output_path.with_suffix(".bin")):
        if path.exists():
            path.unlink()

    log(f"Debug log path: {debug_log_path}")
    log("Fetching root tileset and terrain elevation...")
    root_url = build_tile_url("root.json", API_KEY)
    _, root_tileset = fetch_tile_or_json(root_url)
    center_height = fetch_center_height(latitude, longitude)

    viewpoints = camera_viewpoints(latitude, longitude, radius_meters, grid_steps, center_height)
    cell_spacing = (2.0 * radius_meters / max(1, grid_steps - 1)) if grid_steps > 1 else radius_meters
    span_half_m = cell_spacing * 0.675

    log(
        f"Sweep: {len(viewpoints)} viewpoints, {samples_per_axis}x{samples_per_axis}x"
        f"{len(SURFACE_HEIGHT_OFFSETS)} samples each, {workers} workers."
    )

    seen_hashes: set[bytes] = set()
    downloaded_tiles = 0
    total_raw_matches = 0
    total_ray_errors = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                download_camera_viewpoint,
                point,
                root_tileset,
                root_url,
                center_height,
                span_half_m,
                samples_per_axis,
                dbg,
            )
            for point in viewpoints
        ]
        for future in as_completed(futures):
            completed += 1
            index, lat, lon, matches_by_hash, sample_count, raw_match_count, error_count, elapsed = (
                future.result()
            )
            total_raw_matches += raw_match_count
            total_ray_errors += error_count

            new_tiles = 0
            for digest, glb_bytes in matches_by_hash.items():
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)
                part_path = parts_path / f"tile_{downloaded_tiles + 1:03d}.glb"
                part_path.write_bytes(glb_bytes)
                downloaded_tiles += 1
                new_tiles += 1

            log(
                f"[{completed}/{len(viewpoints)}] VP {index} ({lat:.6f}, {lon:.6f}) "
                f"{sample_count} samples in {elapsed:.2f}s -> +{new_tiles} tiles ({downloaded_tiles} total)."
            )

    stats = get_fetch_stats()
    if not downloaded_tiles:
        log(f"Error: No GLB tiles were fetched. Fetch stats: {stats}")
        return

    log(f"\nMerging {downloaded_tiles} unique tiles...")
    merge_started_at = time.perf_counter()
    merge_gltf(parts_path, output_path)
    merge_elapsed = time.perf_counter() - merge_started_at
    total_elapsed = time.perf_counter() - started_at
    dbg.info("Fetch stats summary: %s", stats)
    log(
        f"Saved {output_path} (merge: {merge_elapsed:.2f}s, total: {total_elapsed:.2f}s, "
        f"raw_hits: {total_raw_matches}, errors: {total_ray_errors}, "
        f"net_reqs: {stats['network_requests']}, cache_hits: {stats['cache_hits']}, "
        f"json: {stats['json_fetches']}, glb: {stats['glb_fetches']})."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download Google 3D Tiles using moving viewpoints across a surface."
    )
    parser.add_argument("--latitude", type=float, default=CENTER_LAT)
    parser.add_argument("--longitude", type=float, default=CENTER_LON)
    parser.add_argument("--radius-meters", type=float, default=100.0)
    parser.add_argument("--grid-steps", type=int, default=8)
    parser.add_argument("--samples-per-axis", type=int, default=6)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACTS_DIR)
    args = parser.parse_args()
    if args.workers < 1 or args.grid_steps < 1 or args.samples_per_axis < 1:
        parser.error("--workers, --grid-steps, and --samples-per-axis must be at least 1")
    artifact_dir = args.artifact_dir.resolve()
    with (BASE_DIR / "log.txt").open("a", encoding="utf-8") as log_file:
        tee = OutputTee(sys.__stdout__, log_file)
        with redirect_stdout(tee), redirect_stderr(tee):
            main(
                args.workers,
                args.latitude,
                args.longitude,
                args.radius_meters,
                args.grid_steps,
                args.samples_per_axis,
                artifact_dir,
            )