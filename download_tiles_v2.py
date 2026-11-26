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
SURFACE_HEIGHT_OFFSETS = (-50.0, 0.0, 25.0, 50.0, 75.0, 100.0, 150.0)


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
    lon_delta = east_m / (111320.0 * math.cos(math.radians(lat)))
    return lat + lat_delta, lon + lon_delta


def surface_targets(
    lat: float,
    lon: float,
    terrain_height: float,
    footprint_half_m: float,
) -> list[tuple[float, float, float]]:
    targets: list[tuple[float, float, float]] = []

    ground_offsets = (
        -footprint_half_m,
        -0.5 * footprint_half_m,
        0.0,
        0.5 * footprint_half_m,
        footprint_half_m,
    )
    for north_m in ground_offsets:
        for east_m in ground_offsets:
            target_lat, target_lon = offset_latlon(lat, lon, east_m, north_m)
            for height_offset in SURFACE_HEIGHT_OFFSETS:
                targets.append((target_lat, target_lon, terrain_height + height_offset))

    return targets


def camera_viewpoints(lat: float, lon: float, radius_m: float, steps: int, center_height: float):
    viewpoints = []
    camera_height = center_height + CAMERA_EYE_OFFSET
    idx = 1

    for north_m in (-radius_m + (2 * radius_m * i / max(1, steps - 1)) for i in range(steps)):
        for east_m in (-radius_m + (2 * radius_m * i / max(1, steps - 1)) for i in range(steps)):
            viewpoint_lat, viewpoint_lon = offset_latlon(lat, lon, east_m, north_m)
            viewpoints.append((idx, viewpoint_lat, viewpoint_lon, camera_height))
            idx += 1

    return viewpoints


def summarize_targets(targets: list[tuple[float, float, float]]) -> str:
    if not targets:
        return "count=0"
    lats = [t[0] for t in targets]
    lons = [t[1] for t in targets]
    heights = [t[2] for t in targets]
    return (
        f"count={len(targets)} "
        f"lat=[{min(lats):.6f}, {max(lats):.6f}] "
        f"lon=[{min(lons):.6f}, {max(lons):.6f}] "
        f"h=[{min(heights):.1f}m, {max(heights):.1f}m]"
    )


def download_camera_viewpoint(
    viewpoint,
    root_tileset: dict,
    root_url: str,
    terrain_height: float,
    footprint_half_m: float,
    dbg: logging.Logger,
):
    index, lat, lon, camera_height = viewpoint
    started_at = time.perf_counter()
    unique_matches: dict[bytes, bytes] = {}
    raw_match_count = 0
    error_count = 0

    targets = surface_targets(lat, lon, terrain_height, footprint_half_m)
    dbg.debug(
        "VP %d start lat=%.6f lon=%.6f cam_h=%.1f terrain_h=%.1f %s",
        index,
        lat,
        lon,
        camera_height,
        terrain_height,
        summarize_targets(targets),
    )

    for sample_idx, (target_lat, target_lon, target_height) in enumerate(targets):
        try:
            target_ecef = latlon_to_ecef(target_lat, target_lon, target_height)
            leaf_glbs = traverse_for_leaf_glbs(root_tileset["root"], target_ecef, API_KEY, root_url)
        except Exception as exc:
            error_count += 1
            dbg.error(
                "VP %d sample=%d target=(%.6f, %.6f, %.1f) traversal error: %s\n%s",
                index,
                sample_idx,
                target_lat,
                target_lon,
                target_height,
                exc,
                traceback.format_exc(),
            )
            continue

        for glb_bytes in leaf_glbs:
            raw_match_count += 1
            digest = hashlib.sha256(glb_bytes).digest()
            if digest not in unique_matches:
                unique_matches[digest] = glb_bytes

    elapsed = time.perf_counter() - started_at
    dbg.info(
        "VP %d done in %.2fs: raw_hits=%d unique_in_vp=%d errors=%d",
        index,
        elapsed,
        raw_match_count,
        len(unique_matches),
        error_count,
    )
    return (
        index,
        lat,
        lon,
        unique_matches,
        len(targets),
        raw_match_count,
        error_count,
        elapsed,
    )


def main(
    workers: int,
    latitude: float,
    longitude: float,
    radius_meters: float,
    grid_steps: int,
    artifact_dir: Path,
):
    global OUTPUT_PATH, PARTS_PATH
    started_at = time.perf_counter()
    output_path = artifact_dir / "stitched.gltf"
    parts_path = artifact_dir / "parts"
    debug_log_path = artifact_dir / "debug.log"
    parts_path.mkdir(parents=True, exist_ok=True)
    dbg = setup_debug_logger(debug_log_path)

    dbg.info(
        "Starting sweep: lat=%.6f lon=%.6f radius_m=%.1f grid_steps=%d workers=%d artifact_dir=%s",
        latitude,
        longitude,
        radius_meters,
        grid_steps,
        workers,
        artifact_dir,
    )

    for filename in os.listdir(parts_path):
        if filename.startswith("tile_") and filename.endswith(".glb"):
            (parts_path / filename).unlink()
    for path in (output_path, output_path.with_suffix(".bin")):
        if path.exists():
            path.unlink()

    log(f"Debug log path: {debug_log_path}")
    log("Fetching root tileset and terrain elevation...")
    root_url = build_tile_url("root.json", API_KEY)
    try:
        _, root_tileset = fetch_tile_or_json(root_url)
        center_height = fetch_center_height(latitude, longitude)
    except Exception as exc:
        dbg.critical("Failed fetching root tileset or center elevation: %s\n%s", exc, traceback.format_exc())
        raise

    dbg.info(
        "Root keys=%s center_height=%.2f camera_height=%.2f",
        list(root_tileset.keys()) if isinstance(root_tileset, dict) else type(root_tileset),
        center_height,
        center_height + CAMERA_EYE_OFFSET,
    )

    viewpoints = camera_viewpoints(latitude, longitude, radius_meters, grid_steps, center_height)
    footprint_half_m = radius_meters / max(1, grid_steps - 1)
    dbg.info("Generated %d viewpoints; footprint_half_m=%.2f", len(viewpoints), footprint_half_m)

    log(
        f"Using camera elevation {center_height + CAMERA_EYE_OFFSET:.1f} m "
        f"({center_height:.1f} m terrain + {CAMERA_EYE_OFFSET:.1f} m eye offset)."
    )
    log(
        f"Surface sweep: {len(viewpoints)} moving viewpoints, local footprint half-width "
        f"{footprint_half_m:.1f} m, and {len(SURFACE_HEIGHT_OFFSETS)} height bands."
    )
    log(f"Using {workers} concurrent camera workers.")

    seen_hashes: set[bytes] = set()
    downloaded_tiles = 0
    total_raw_matches = 0
    total_ray_errors = 0
    zero_new_viewpoints: list[int] = []
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                download_camera_viewpoint,
                point,
                root_tileset,
                root_url,
                center_height,
                footprint_half_m,
                dbg,
            )
            for point in viewpoints
        ]
        for future in as_completed(futures):
            completed += 1
            try:
                (
                    index,
                    lat,
                    lon,
                    matches_by_hash,
                    ray_count,
                    raw_match_count,
                    error_count,
                    elapsed,
                ) = future.result()
            except Exception as exc:
                total_ray_errors += 1
                dbg.error("Future failed: %s\n%s", exc, traceback.format_exc())
                log(f"[{completed}/{len(viewpoints)}] Worker raised an exception (see debug.log): {exc}")
                continue

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
                dbg.debug(
                    "Saved tile %s sha256=%s bytes=%d from VP %d",
                    part_path.name,
                    digest.hex()[:16],
                    len(glb_bytes),
                    index,
                )

            if new_tiles == 0:
                zero_new_viewpoints.append(index)

            log(
                f"[{completed}/{len(viewpoints)}] Viewpoint {index} "
                f"({lat:.6f}, {lon:.6f}) tested {ray_count} points in {elapsed:.2f}s "
                f"-> {new_tiles} new unique tiles ({downloaded_tiles} total)."
            )

    dbg.info(
        "Sweep complete: viewpoints=%d raw_matches=%d unique_tiles=%d ray_errors=%d zero_new_vps=%s",
        len(viewpoints),
        total_raw_matches,
        downloaded_tiles,
        total_ray_errors,
        zero_new_viewpoints,
    )

    if not downloaded_tiles:
        dbg.error("No GLB tiles were fetched.")
        log("Error: No GLB tiles were fetched.")
        return

    log(f"\nMerging {downloaded_tiles} unique tiles while preserving glTF node transforms...")
    merge_started_at = time.perf_counter()
    try:
        merge_gltf(parts_path, output_path)
    except Exception as exc:
        dbg.critical("merge_gltf failed: %s\n%s", exc, traceback.format_exc())
        raise

    merge_elapsed = time.perf_counter() - merge_started_at
    total_elapsed = time.perf_counter() - started_at
    dbg.info("Merge succeeded in %.2fs -> %s (total %.2fs)", merge_elapsed, output_path, total_elapsed)
    log(f"Successfully saved combined street GLTF to: {output_path}")
    log(
        f"Merge took {merge_elapsed:.2f}s; "
        f"total camera download and merge took {total_elapsed:.2f}s."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download Google 3D Tiles using moving viewpoints across a surface."
    )
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
            main(
                args.workers,
                args.latitude,
                args.longitude,
                args.radius_meters,
                args.grid_steps,
                artifact_dir,
            )