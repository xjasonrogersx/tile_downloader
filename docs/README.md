# Cesium Native Python bindings

This workspace contains Cesium Native and the Python bindings built with CMake,
Ninja, and pybind11.

## Ubuntu prerequisites

Install the compiler, CMake, Ninja, Python development headers, and Git:

```bash
sudo apt update
sudo apt install -y build-essential cmake ninja-build git python3-dev python3-pip pkg-config
```

The Python build also needs Python 3.12 or newer and NumPy. The Python package
declares NumPy, pybind11, scikit-build-core, and pathspec as build or runtime
dependencies, so pip installs them during the build.

## Build Cesium Native

For a standalone Cesium Native checkout:

```bash
git clone --recurse-submodules https://github.com/CesiumGS/cesium-native.git
cd cesium-native
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

If the repository was cloned without submodules, initialize them first:

```bash
git submodule update --init --recursive
```

The first native build downloads and compiles several dependencies through the
project's vcpkg setup and can take a while. Later builds reuse the dependency
cache.

## Build and install the Python bindings

Clone the bindings with their pinned Cesium Native and libzip submodules:

```bash
git clone --recurse-submodules https://github.com/calebbuffa/cesium-python.git
cd cesium-python
git submodule update --init --recursive
python3 -m pip install . --break-system-package
```

The `--break-system-package` option is needed when installing into the
container's system Python. A virtual environment is preferred on a normal
Ubuntu installation:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

The first Python build compiles the pinned Cesium Native dependency graph and
the bindings, so it may take 20 minutes or more. The build output is placed in
`cesium-python/build/`, and subsequent builds are incremental.

## Verify the installation

```bash
python3 -c "import cesium; print('cesium import succeeded')"
```

## Troubleshooting

If CMake reports that `pybind11Config.cmake` cannot be found, make sure the
bindings checkout includes the current `cmake/Dependencies.cmake`, which finds
pybind11 in pip's isolated build environment. Then remove only the generated
build directory and retry:

```bash
rm -rf build
python3 -m pip install . --break-system-package
```

start location 51.7451501,-0.4661585

## Download Google Photorealistic 3D Tiles

Enable the Map Tiles API for a Google Cloud project and use a restricted API
key. The standalone downloader uses the same default location and writes the
first matching GLB tile to `google_photorealistic_tile.glb`:

```bash
export GOOGLE_MAPS_API_KEY='your-key'
python3 google_photorealistic_tile.py
```

Override the location or output path when needed:

```bash
python3 google_photorealistic_tile.py \
	--longitude -0.4661585 \
	--latitude 51.7451501 \
	--output tile.glb
```

