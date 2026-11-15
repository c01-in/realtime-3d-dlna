"""Run the local oVDA top-and-bottom DLNA server."""

import argparse
import asyncio
from contextlib import asynccontextmanager
import html
import ipaddress
import json
import logging
import os
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path, default=HERE / "data")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--media", type=Path, action="append", default=[])
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--name", default="Realtime 3D DLNA")
    parser.add_argument("--lan-ip", default=os.getenv("LIVE3D_LAN_IP", ""))
    parser.add_argument("--ffmpeg-dir", type=Path)
    parser.add_argument("--cuda-headers", type=Path)
    parser.add_argument("--no-ssdp", action="store_true")
    args = parser.parse_args()
    if args.config:
        settings = json.loads(args.config.read_text(encoding="utf-8-sig"))
        for key in ("data_dir", "model_dir", "ffmpeg_dir", "cuda_headers"):
            if settings.get(key):
                setattr(args, key, Path(settings[key]))
        for key in ("port", "name", "lan_ip", "no_ssdp"):
            if key in settings:
                setattr(args, key, settings[key])
        args.media = [Path(path) for path in settings["media"]]
        sys.path[:0] = settings.get("python_path", [])
    if not args.media:
        parser.error("Set at least one --media directory")
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    if args.lan_ip:
        ipaddress.IPv4Address(args.lan_ip)
    return args


def run():
    args = arguments()
    data = args.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None:
        sys.stdout = open(data / "server.log", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = sys.stdout
    logging.basicConfig(level=os.getenv("LIVE3D_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s  %(message)s")
    roots = list(dict.fromkeys(path.resolve() for path in args.media))
    for root in roots:
        if not root.is_dir():
            raise FileNotFoundError(f"Cannot read media directory: {root}")
    thru = data / "upstream/thru3d"
    ovda = data / "upstream/ovda"
    checkpoint = (args.model_dir or data / "models") / "oVDA_c16.pth"
    if not all(path.is_file() for path in (thru / "config.py", ovda / "configs/oVDA_c16.yaml", checkpoint)):
        raise RuntimeError("Runtime assets missing. Run assets.py before starting the service.")
    if args.ffmpeg_dir:
        os.environ["PATH"] = os.pathsep.join((str(args.ffmpeg_dir), os.environ["PATH"]))
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(f"Cannot find {command}. Install FFmpeg with support for HEVC NVENC.")
    os.environ.update({
        "PT_HTTP_PORT": str(args.port), "PT_SERVER_NAME": html.escape(args.name),
        "PT_VIDEO_DIR": str(data / "empty-library"),
        "PT_RUNTIME_CACHE_DIR": str(data / "cache"),
        "MPLCONFIGDIR": str(data / "mpl"),
    })
    if args.lan_ip:
        os.environ["PT_LAN_IP"] = args.lan_ip
    sys.path[:0] = [str(thru), str(ovda)]
    if args.cuda_headers:
        os.environ["CUDA_PATH"] = str(args.cuda_headers)
    elif not os.getenv("CUDA_PATH"):
        import nvidia.cuda_runtime
        os.environ["CUDA_PATH"] = str(Path(next(iter(nvidia.cuda_runtime.__path__))))
    dll_directory = None
    if os.name == "nt":
        import torch
        torch_lib = Path(torch.__file__).parent / "lib"
        dll_directory = os.add_dll_directory(str(torch_lib))
        os.environ["PATH"] = os.pathsep.join((str(torch_lib), os.environ["PATH"]))

    import config
    from media_library import MediaLibrary, build_media_roots
    config.MEDIA_LIBRARY = MediaLibrary(build_media_roots(roots))
    config.VIDEO_EXTS.update({".m2ts", ".mpg", ".mpeg", ".wmv", ".flv"})
    import uvicorn
    from dlna.connection_manager import handle_soap
    from dlna.descriptions import cds_scpd, cm_scpd, device_description
    from dlna.ssdp import SSDPServer
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, Response
    from library import Library, VIDEO_EXTENSIONS, directory_response
    from ovda import OvdaEngine
    from stream import SeekableVideo, seekable_response
    from utils.subtitles import find_external_subtitles, subtitle_response_mime
    from video import GpuStereoRenderer, HOLE_FILL, STEREO_STRENGTH, DEFAULT_FLAT_FOV_DEG, effective_eye_distance_mm, probe

    engine = OvdaEngine(ovda, checkpoint)
    import numpy as np
    starter = np.full((800, 1920, 3), 64, dtype=np.uint8)
    starter_depth = engine.sequence(1920, 800)(starter)
    if not np.isfinite(starter_depth).all():
        raise RuntimeError("oVDA startup inference returned non-finite depth")
    GpuStereoRenderer(1920, 800, "flat3d", effective_eye_distance_mm(65, STEREO_STRENGTH),
                      HOLE_FILL, DEFAULT_FLAT_FOV_DEG).render(starter, starter_depth)
    library = Library(roots, f"http://{config.LAN_IP}:{args.port}")

    @asynccontextmanager
    async def lifespan(_app):
        ssdp = None if args.no_ssdp else SSDPServer()
        if ssdp:
            ssdp.start()
        try:
            yield
        finally:
            if ssdp:
                ssdp.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    xml = "text/xml; charset=utf-8"
    app.get("/description.xml")(lambda: Response(device_description(), media_type=xml))
    app.get("/cds.xml")(lambda: Response(cds_scpd(), media_type=xml))
    app.get("/cm.xml")(lambda: Response(cm_scpd(), media_type=xml))

    @app.get("/")
    def dashboard():
        return FileResponse(HERE / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ready", "name": args.name, "model": "oVDA c16", "output": "1920×1080 · 上下 3D",
                "gpu": str(engine.device), "discovery": not args.no_ssdp}

    @app.get("/api/library")
    def list_media(folder: str = "0"):
        try:
            return library.browse(folder)
        except (OSError, ValueError) as exc:
            raise HTTPException(404, "Media folder unavailable") from exc

    @app.post("/control/cds")
    async def directory(request: Request):
        body = await request.body()
        payload, code = await asyncio.to_thread(directory_response, request.headers, body, library)
        return Response(payload, status_code=code, media_type=xml)

    @app.post("/control/cm")
    async def manager(request: Request):
        payload, code = handle_soap(request.headers.get("SOAPAction", ""), await request.body())
        return Response(payload, status_code=code, media_type=xml)

    def source_for(token, legacy=False):
        try:
            if legacy:
                token = library.previous_id(token)
            _, _, source = library.resolve(token)
            if source.suffix.lower() in VIDEO_EXTENSIONS and source.is_file():
                return source
        except OSError:
            pass
        raise HTTPException(404, "Video unavailable")

    @app.api_route("/nas-subtitles/{token}/{filename}", methods=["GET", "HEAD"])
    @app.api_route("/subtitles/{token}/{filename}", methods=["GET", "HEAD"])
    def subtitles(request: Request, token: str, filename: str, fmt: str | None = None):
        source = source_for(token, request.url.path.startswith("/nas-subtitles/"))
        track = next((entry for entry in find_external_subtitles(source) if entry.path.name == filename), None)
        if track is None or not track.path.resolve().is_relative_to(source.parent.resolve()):
            raise HTTPException(404, "Subtitle unavailable")
        return FileResponse(track.path, media_type=subtitle_response_mime(track.path, fmt))

    def asset_for(token, legacy):
        source = source_for(token, legacy)
        stat = source.stat()
        revision = stat.st_mtime_ns, stat.st_size
        return SeekableVideo(source, probe(source, revision), revision, engine)

    @app.api_route("/nas-live/{token}/{filename}", methods=["GET", "HEAD"])
    @app.api_route("/media/{token}/{filename}", methods=["GET", "HEAD"])
    async def media(request: Request, token: str, filename: str):
        try:
            asset = await asyncio.to_thread(asset_for, token, request.url.path.startswith("/nas-live/"))
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(422, "Unable to read video stream") from exc
        return await seekable_response(request, asset)

    logging.info("Serving %s on %s with oVDA c16", args.name, library.address)
    try:
        uvicorn.run(app, host="0.0.0.0", port=args.port, access_log=False, timeout_graceful_shutdown=4)
    finally:
        if dll_directory:
            dll_directory.close()


if __name__ == "__main__":
    try:
        run()
    except Exception:
        logging.exception("DLNA service failed")
        raise SystemExit(1)
