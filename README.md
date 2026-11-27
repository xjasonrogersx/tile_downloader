# Google 3D Tile Downloader

Downloads Google Photorealistic 3D Tiles for selected map regions, stores the
individual GLB tiles in a shared cache, and merges them into glTF models.

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

The main page links to the **Coverage grid** at
`http://127.0.0.1:8000/grid`. If port 8000 is already in use, the server can be
started on another port from Python, for example:

```bash
python3 -c 'import server; server.ThreadingHTTPServer(("127.0.0.1", 8001), server.Handler).serve_forever()'
```

The downloader estimates the center terrain elevation through the Open-Meteo
Elevation API. It then probes several heights around that value and collects
all matching street-level tile branches. This helps account for differences
between DEM elevation and the vertical coordinates used by the 3D Tiles data,
and helps retain both road-level and elevated building geometry.

The web UI includes a **Grid steps** slider for controlling grid sampling
density. Higher values improve coverage but require more tile requests and may
take longer. For camera sweep, it controls the number of moving viewpoints per
axis across the selected square.

## Coverage grid

The coverage grid provides a top-down view of all downloaded areas. It uses
25-metre square cells, colors cells covered by one or more areas, and displays
the number of distinct cached tile hashes associated with each covered cell.
Click a cell to inspect its centroid, dimensions, cached tiles, GLB size,
node/mesh counts, and local geometry height bounds.

Each tile in the selected-cell panel has a **View tile** action that opens the
cached GLB in a model viewer. **Merge cell tiles and view** creates a temporary
manifest for the selected hashes, runs `merge_gltf.py` in a subprocess, and
opens the resulting `cell_merge.gltf` in the same viewer.

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

- `metadata.json`: center, radius, method, and ordered `tile_hashes`
- `stitched.gltf` and `stitched.bin`: stitched model shown by the web viewer

Cell merges are written under `artifacts/cell_merges/<merge-id>/` and contain:

- `metadata.json`: the selected cell's tile hash manifest
- `cell_merge.gltf` and `cell_merge.bin`: the merged cell model

The source GLBs are stored once in the shared `artifacts/tiles/` directory,
named by their SHA-256 hash. When another area finds the same tile, it reuses
the cached file instead of downloading it again. The area manifest in
`metadata.json` determines which tiles are merged and in what order.

Downloader stdout and stderr are appended to `log.txt` while also being shown
in the terminal. The log is useful for checking sample progress, missing tile
points, API failures, and merge errors.

The local server exposes these useful routes:

- `/`: map downloader and area history
- `/grid`: cached-area coverage grid
- `/area?area=<area-id>`: area metadata and stitched model viewer
- `/api/grid`: grid data, cell metadata, and cached tile details
- `/api/merge-cell`: merge a validated list of cached tile hashes

If a road or roof is missing, repeat the download with a larger radius and
ensure the selected region covers the complete feature. A denser grid can be
requested from the command line by increasing `--grid-steps`.
