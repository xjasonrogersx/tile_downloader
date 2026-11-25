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

The web UI samples the region at approximately 15 metre intervals. Larger
radii therefore require more tile requests and may take longer.

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
