# Google 3D Tile Downloader

Downloads Google Photorealistic 3D Tiles for a selected map region, saves the
individual GLB tiles, and merges them into a single glTF model.

## Requirements

- Python 3.10 or newer
- A Google Maps Platform API key with the Map Tiles API enabled
- Python packages: `numpy`, `requests`, `trimesh`, and `Pillow`

Install the Python dependencies with:

```bash
python3 -m pip install numpy requests trimesh pillow
```

Put the Google API key in `token.txt`, or set:

```bash
export GOOGLE_3D_TILES_API_KEY="your-key"
```

## Start the map downloader

```bash
python3 server.py
```

Open `http://127.0.0.1:8000` and click the map to set the center of the
download region. Choose a radius and start the download.

The downloader estimates the center terrain elevation through the Open-Meteo
Elevation API. It then probes several heights around that value and collects
all matching street-level tile branches. This helps account for differences
between DEM elevation and the vertical coordinates used by the 3D Tiles data,
and helps retain both road-level and elevated building geometry.

The web UI includes a **Grid steps** slider for controlling grid sampling
density. Higher values improve coverage but require more tile requests and may
take longer. For camera sweep, it controls the number of moving viewpoints per
axis across the selected square.

## Download methods

The web UI provides two methods, with **Camera sweep** selected by default:

- **Grid sampling:** the original method. It samples geographic points at
	several heights and downloads matching street-level tiles.
- **Camera sweep:** `download_tiles_v2.py` moves bounded viewpoints across the
	selected area. Each viewpoint samples a local surface patch at several
	heights. Matching tiles are deduplicated before merging.

Camera sweep is intended to improve coverage of surrounding sides and roofs,
but it is still a downloader-side geometric approximation rather than a full
Cesium renderer. It can make more requests than a small grid selection.

## Run from the command line

```bash
python3 download_tiles.py \
	--latitude 51.7451423 \
	--longitude -0.466216 \
	--radius-meters 100 \
	--grid-steps 15 \
	--workers 8
```

The command-line downloader writes files under `artifacts/` by default. Use
`--artifact-dir` to choose another output directory. `--workers` controls the
number of grid points traversed concurrently; lower it if the API rate-limits
requests.

## Output and debugging

Each download creates an area directory containing:

- `parts/tile_*.glb`: downloaded source tiles
- `stitched.gltf` and `stitched.bin`: merged model
- `metadata.json`: center, radius, grid size, and tile count

Downloader stdout and stderr are appended to `log.txt` while also being shown
in the terminal. The log is useful for checking sample progress, missing tile
points, API failures, and merge errors.

If a road or roof is missing, repeat the download with a larger radius and
ensure the selected region covers the complete feature. A denser grid can be
requested from the command line by increasing `--grid-steps`.

## Fill gaps in an existing area

The area viewer shows the merged result, but it cannot identify missing tiles
automatically. `fill_gaps.py` scans an existing area again using a denser
geographic grid and the same vertical probes used by the grid downloader. It
keeps the existing GLBs, adds only new content, and rebuilds the merged model.

Preview the scan size first:

```bash
python3 fill_gaps.py area-20261125-223411-1dd082 --spacing 20 --dry-run
```

Run the scan:

```bash
python3 fill_gaps.py area-20261125-223411-1dd082 --spacing 20 --workers 8
```

Use a larger spacing such as `40` for a faster broad pass, or a smaller
spacing such as `10` around a narrow missing road. The area URL then remains
the same; refresh it after the scan finishes.
