#!/usr/bin/env python3
"""Local map UI for selecting, downloading, and viewing a 3D tile region."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlparse

BASE_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = BASE_DIR / "artifacts"
HOST = "127.0.0.1"
PORT = 8000
LOG_PATH = BASE_DIR / "log.txt"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

job_lock = threading.Lock()
log_lock = threading.Lock()
download_process = None
job = {"id": None, "area_id": None, "status": "idle", "message": "Choose a region on the map.", "progress": 0, "progress_total": 0, "lines": [], "artifacts": []}


def write_log(message: str) -> None:
    with log_lock:
        with LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(message.rstrip() + "\n")


def estimate_region(radius_meters: float) -> dict[str, int]:
    # Keep samples close enough to catch narrow roads and tile footprints.
    grid_steps = max(3, min(101, math.ceil((radius_meters * 2) / 15) + 1))
    return {"grid_steps": grid_steps, "samples": grid_steps * grid_steps}


def area_metadata(area_dir: Path) -> dict:
    metadata_path = area_dir / "metadata.json"
    if metadata_path.exists():
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    return {"area_id": area_dir.name}


def artifact_list(area_id: str | None = None) -> list[dict[str, str | int]]:
    files = []
    area_dirs = [ARTIFACTS_DIR / area_id] if area_id else sorted(
        path for path in ARTIFACTS_DIR.iterdir() if path.is_dir()
    )
    for area_dir in area_dirs:
        if not area_dir.exists():
            continue
        metadata = area_metadata(area_dir)
        for path in sorted((area_dir / "parts").glob("*.glb")):
            files.append({"name": path.name, "size": path.stat().st_size, "kind": "tile", "area_id": area_dir.name, "area_name": metadata.get("name", area_dir.name), "details": f"/area?area={quote(area_dir.name)}"})
        for name in ("stitched.gltf", "stitched.bin", "merged.gltf", "merged.bin"):
            path = area_dir / name
            if path.exists():
                files.append({"name": name, "size": path.stat().st_size, "kind": "merged", "area_id": area_dir.name, "area_name": metadata.get("name", area_dir.name), "details": f"/area?area={quote(area_dir.name)}"})
    return files


def write_metadata(area_dir: Path, values: dict) -> None:
    metadata_path = area_dir / "metadata.json"
    existing = area_metadata(area_dir)
    existing.update(values)
    metadata_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


def run_download(latitude: float, longitude: float, radius_meters: float, grid_steps: int, job_id: str, area_id: str) -> None:
    global download_process
    area_dir = ARTIFACTS_DIR / area_id
    command = [
        sys.executable,
        "-u",
        str(BASE_DIR / "download_tiles.py"),
        "--latitude", str(latitude),
        "--longitude", str(longitude),
        "--radius-meters", str(radius_meters),
        "--grid-steps", str(grid_steps),
        "--artifact-dir", str(area_dir),
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with job_lock:
            download_process = process
        assert process.stdout is not None
        for line in process.stdout:
            with job_lock:
                if job["id"] == job_id:
                    match = re.search(r"\[(\d+)/(\d+)\]", line)
                    if match:
                        job["progress"] = int(match.group(1))
                        job["progress_total"] = int(match.group(2))
                    job["lines"].append(line.rstrip())
                    job["message"] = line.strip() or job["message"]
        return_code = process.wait()
        with job_lock:
            if job["id"] == job_id:
                was_stopped = job["status"] == "stopping"
                job["status"] = "stopped" if was_stopped else "complete" if return_code == 0 else "error"
                job["message"] = "Download stopped. Partial artifacts were kept." if was_stopped else "Download and merge complete." if return_code == 0 else f"Download failed with code {return_code}."
                if return_code == 0:
                    stitched = area_dir / "stitched.gltf"
                    stitched_bin = area_dir / "stitched.bin"
                    if stitched.exists():
                        shutil.copy2(stitched, area_dir / "merged.gltf")
                    if stitched_bin.exists():
                        shutil.copy2(stitched_bin, area_dir / "merged.bin")
                    write_metadata(area_dir, {"completed_at": datetime.now(timezone.utc).isoformat(), "tile_count": len(list((area_dir / "parts").glob("*.glb")))})
                job["artifacts"] = artifact_list(area_id)
            download_process = None
    except Exception as exc:
        write_log(f"download error: {exc}")
        with job_lock:
            if job["id"] == job_id:
                job["status"] = "error"
                job["message"] = str(exc)
            download_process = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        write_log(f"http: {self.address_string()} - {format % args}")

    def send_json(self, value: object, status: int = 200) -> None:
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            with job_lock:
                self.send_json(dict(job))
            return
        if parsed.path == "/api/areas":
            areas = []
            for area_dir in sorted((path for path in ARTIFACTS_DIR.iterdir() if path.is_dir()), reverse=True):
                metadata = area_metadata(area_dir)
                areas.append({**metadata, "details": f"/area?area={quote(area_dir.name)}"})
            self.send_json(areas)
            return
        if parsed.path == "/api/artifacts":
            self.send_json(artifact_list())
            return
        if parsed.path == "/api/estimate":
            query = parse_qs(parsed.query)
            radius = float(query.get("radius", [100])[0])
            self.send_json(estimate_region(radius))
            return
        if parsed.path == "/area":
            self.send_area(parse_qs(parsed.query).get("area", [""])[0])
            return
        if parsed.path == "/viewer":
            self.send_viewer(parse_qs(parsed.query).get("area", [""])[0])
            return
        if parsed.path == "/":
            self.send_file(BASE_DIR / "web" / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/files/"):
            parts = Path(parsed.path.removeprefix("/files/")).parts
            if len(parts) != 2:
                self.send_error(404)
                return
            candidate = ARTIFACTS_DIR / parts[0] / parts[1]
            if candidate.exists() and candidate.is_file() and candidate.suffix in {".glb", ".gltf", ".bin"} and candidate.parent.name == parts[0]:
                content_type = "model/gltf-binary" if candidate.suffix == ".glb" else "model/gltf+json" if candidate.suffix == ".gltf" else "application/octet-stream"
                self.send_file(candidate, content_type, download=True)
                return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/area-details":
            self.update_area_details()
            return
        if path == "/api/stop":
            self.stop_download()
            return
        if path == "/api/delete-area":
            self.delete_area()
            return
        if path != "/api/start":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            latitude = float(data["latitude"])
            longitude = float(data["longitude"])
            radius = max(5.0, min(1000.0, float(data["radiusMeters"])))
            estimate = estimate_region(radius)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid region parameters."}, 400)
            return
        with job_lock:
            if job["status"] == "running":
                self.send_json({"error": "A download is already running."}, 409)
                return
            job_id = uuid.uuid4().hex
            area_id = "area-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + job_id[:6]
            area_dir = ARTIFACTS_DIR / area_id
            area_dir.joinpath("parts").mkdir(parents=True, exist_ok=True)
            write_metadata(area_dir, {"area_id": area_id, "name": area_id, "notes": "", "created_at": datetime.now(timezone.utc).isoformat(), "latitude": latitude, "longitude": longitude, "radius_meters": radius, "grid_steps": estimate["grid_steps"]})
            job.update({"id": job_id, "area_id": area_id, "status": "running", "message": "Starting download...", "progress": 0, "progress_total": estimate["samples"], "lines": [], "artifacts": []})
        thread = threading.Thread(target=run_download, args=(latitude, longitude, radius, estimate["grid_steps"], job_id, area_id), daemon=True)
        thread.start()
        self.send_json({"id": job_id, "area_id": area_id, **estimate})

    def stop_download(self) -> None:
        global download_process
        with job_lock:
            if job["status"] != "running" or download_process is None:
                self.send_json({"error": "No download is running."}, 409)
                return
            job["status"] = "stopping"
            job["message"] = "Stopping download..."
            process = download_process
        process.terminate()
        self.send_json({"status": "stopping"})

    def update_area_details(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            area_id = Path(str(data["area_id"])).name
            if area_id != data["area_id"]:
                raise ValueError("Invalid area id")
            write_metadata(ARTIFACTS_DIR / area_id, {"name": str(data.get("name", area_id))[:120], "notes": str(data.get("notes", ""))[:2000]})
            self.send_json(area_metadata(ARTIFACTS_DIR / area_id))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid area details."}, 400)

    def delete_area(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            area_id = str(data["area_id"])
            safe_area = Path(area_id).name
            area_dir = ARTIFACTS_DIR / safe_area
            if safe_area != area_id or not safe_area.startswith("area-") or not area_dir.is_dir():
                raise ValueError("Invalid area id")
            with job_lock:
                if job.get("area_id") == safe_area and job.get("status") in {"running", "stopping"}:
                    self.send_json({"error": "Stop the active download before deleting its area."}, 409)
                    return
            shutil.rmtree(area_dir)
            self.send_json({"deleted": safe_area})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid area id."}, 400)

    def send_file(self, path: Path, content_type: str, download: bool = False) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)

    def send_area(self, area_id: str) -> None:
        safe_area = Path(area_id).name
        area_dir = ARTIFACTS_DIR / safe_area
        if not area_dir.is_dir():
            self.send_error(404)
            return
        html = AREA_HTML.replace("__AREA_ID__", safe_area).replace("__AREA_JSON__", json.dumps(area_metadata(area_dir)))
        self.send_html(html)

    def send_viewer(self, area_id: str) -> None:
        safe_area = Path(area_id).name
        metadata = area_metadata(ARTIFACTS_DIR / safe_area)
        model_url = "/files/" + quote(safe_area) + "/merged.gltf"
        html = VIEWER_HTML.replace("__MODEL_URL__", model_url).replace("__MODEL_NAME__", str(metadata.get("name", safe_area)))
        self.send_html(html)

    def send_html(self, html: str) -> None:
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


VIEWER_HTML = """<!doctype html><html><head><meta charset='utf-8'><title>Tile viewer</title>
<script type='module' src='https://ajax.googleapis.com/ajax/libs/model-viewer/4.0.0/model-viewer.min.js'></script>
<style>html,body{height:100%;margin:0;background:#111;color:#eee;font:14px system-ui}header{height:52px;padding:0 20px;display:flex;align-items:center;border-bottom:1px solid #333}model-viewer{width:100%;height:calc(100% - 53px);background:#16191c}a{color:#8ed0c0;margin-left:auto}</style></head>
<body><header><strong>__MODEL_NAME__</strong><a href='/'>Back to map</a></header><model-viewer src='__MODEL_URL__' camera-controls auto-rotate shadow-intensity='0.8' exposure='1.1'></model-viewer></body></html>"""

AREA_HTML = """<!doctype html><html><head><meta charset='utf-8'><title>Area details</title>
<script type='module' src='https://ajax.googleapis.com/ajax/libs/model-viewer/4.0.0/model-viewer.min.js'></script>
<style>body{margin:0;background:#f4f1e9;color:#17211f;font:15px system-ui}main{max-width:1100px;margin:auto;padding:28px}header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}a{color:#23685b;font-weight:600}section{background:#fffdf8;padding:22px;margin-bottom:18px}label{display:block;font-weight:600;margin:12px 0 5px}input,textarea{width:100%;padding:10px;border:1px solid #ccd4cc;font:inherit;box-sizing:border-box}textarea{min-height:110px}button{margin-top:14px;background:#17211f;color:white;border:0;padding:11px 16px;font-weight:600;cursor:pointer}.status{margin-left:10px;color:#52766d}model-viewer{width:100%;height:560px;background:#171b1b}</style></head><body><main><header><h1>Area details</h1><a href='/'>Back to map</a></header><section><form id='details'><label>Name</label><input id='name'><label>Details / notes</label><textarea id='notes'></textarea><button>Save details</button><span class='status' id='saveStatus'></span></form></section><section><h2>Merged model</h2><model-viewer src='/files/__AREA_ID__/merged.gltf' camera-controls auto-rotate shadow-intensity='0.8' exposure='1.1'></model-viewer></section></main><script>const area=__AREA_JSON__;name.value=area.name||area.area_id;notes.value=area.notes||'';details.onsubmit=async e=>{e.preventDefault();const r=await fetch('/api/area-details',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({area_id:area.area_id,name:name.value,notes:notes.value})});saveStatus.textContent=r.ok?'Saved':'Save failed'}</script></body></html>"""

AREA_HTML = """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>Area details</title>
<script type='module' src='https://ajax.googleapis.com/ajax/libs/model-viewer/4.0.0/model-viewer.min.js'></script>
<style>body{margin:0;background:#f4f1e9;color:#17211f;font:15px system-ui}main{max-width:1400px;margin:auto;padding:24px}header{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}a{color:#23685b;font-weight:600}.layout{display:grid;grid-template-columns:minmax(260px,360px) minmax(0,1fr);gap:18px;align-items:start}.panel{background:#fffdf8;padding:22px;min-height:560px}label{display:block;font-weight:600;margin:12px 0 5px}input,textarea{width:100%;padding:10px;border:1px solid #ccd4cc;font:inherit;box-sizing:border-box}textarea{min-height:160px;resize:vertical}button{margin-top:14px;background:#17211f;color:white;border:0;padding:11px 16px;font-weight:600;cursor:pointer}.delete{background:#a94b38}.status{display:block;margin-top:12px;color:#52766d}dl{display:grid;grid-template-columns:auto 1fr;gap:8px;margin:22px 0 0;font-size:13px}dt{color:#65716d}dd{margin:0;text-align:right}model-viewer{width:100%;height:560px;background:#171b1b}@media(max-width:800px){main{padding:14px}.layout{grid-template-columns:1fr}.panel{min-height:0}model-viewer{height:65vh}}</style></head><body><main><header><h1>Area details</h1><a href='/'>Back to map</a></header><div class='layout'><section class='panel'><form id='details'><label for='name'>Name</label><input id='name'><label for='notes'>Details / notes</label><textarea id='notes'></textarea><button>Save details</button><span class='status' id='saveStatus'></span></form><dl><dt>Area ID</dt><dd id='areaId'></dd><dt>Coordinates</dt><dd id='coordinates'></dd><dt>Radius</dt><dd id='radius'></dd></dl><button class='delete' id='delete'>Delete area</button><span class='status' id='deleteStatus'></span></section><section class='panel'><h2>Merged model</h2><model-viewer src='/files/__AREA_ID__/merged.gltf' camera-controls auto-rotate shadow-intensity='0.8' exposure='1.1'></model-viewer></section></div></main><script>const area=__AREA_JSON__;name.value=area.name||area.area_id;notes.value=area.notes||'';areaId.textContent=area.area_id;coordinates.textContent=`${Number(area.latitude).toFixed(5)}, ${Number(area.longitude).toFixed(5)}`;radius.textContent=`${area.radius_meters||'-'} m`;details.onsubmit=async e=>{e.preventDefault();const r=await fetch('/api/area-details',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({area_id:area.area_id,name:name.value,notes:notes.value})});saveStatus.textContent=r.ok?'Saved':'Save failed'};delete.onclick=async()=>{if(!confirm('Delete this area and all downloaded artifacts? This cannot be undone.'))return;delete.disabled=true;const r=await fetch('/api/delete-area',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({area_id:area.area_id})});if(r.ok)location.href='/';else{delete.disabled=false;deleteStatus.textContent=(await r.json()).error||'Delete failed'}}</script></body></html>"""

AREA_HTML = AREA_HTML.replace("delete.onclick", "document.getElementById('delete').onclick").replace("delete.disabled", "document.getElementById('delete').disabled")


if __name__ == "__main__":
    print(f"Open http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
