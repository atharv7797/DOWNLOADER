from __future__ import annotations
 
import asyncio
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import TypedDict
 
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
 
app = FastAPI(title="Fetch backend")
 
# Tighten this to your actual frontend origin before deploying anywhere public.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
 
DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "fetch-downloads"
DOWNLOAD_ROOT.mkdir(exist_ok=True)
 
HEIGHT_BUCKETS = [144, 240, 360, 480, 720, 1080]
 
 
class Quality(TypedDict):
    label: str
    height: int
    approx_mb: float | None
    badge: str | None
 
 
def _base_ydl_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
 
 
def _extract_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(_base_ydl_opts()) as ydl:
        try:
            return ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise HTTPException(status_code=400, detail=f"Couldn't read that URL: {exc}") from exc
 
 
def _available_qualities(info: dict) -> list[Quality]:
    formats = info.get("formats") or []
    heights_present = {f.get("height") for f in formats if f.get("height")}
 
    qualities: list[Quality] = []
    for h in HEIGHT_BUCKETS:
        if not any(existing and existing >= h for existing in heights_present):
            continue
        candidates = [f for f in formats if f.get("height") == h]
        filesize = next(
            (f.get("filesize") or f.get("filesize_approx") for f in candidates if f.get("filesize") or f.get("filesize_approx")),
            None,
        )
        qualities.append(
            {
                "label": f"{h}p",
                "height": h,
                "approx_mb": round(filesize / 1_000_000, 1) if filesize else None,
                "badge": "best" if h == max(HEIGHT_BUCKETS) else ("HD" if h >= 720 else None),
            }
        )
    return qualities
 
 
@app.get("/api/info")
def get_info(url: str = Query(..., description="YouTube video URL")):
    info = _extract_info(url)
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "author": info.get("uploader") or info.get("channel"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "qualities": _available_qualities(info),
    }
 
 
@app.get("/api/download")
async def download(
    url: str = Query(..., description="YouTube video URL"),
    height: int = Query(1080, description="Max vertical resolution, e.g. 1080"),
):
    if height not in HEIGHT_BUCKETS:
        raise HTTPException(status_code=400, detail=f"height must be one of {HEIGHT_BUCKETS}")
 
    job_dir = DOWNLOAD_ROOT / uuid.uuid4().hex
    job_dir.mkdir()
 
    opts = {
        **_base_ydl_opts(),
        "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
        "merge_output_format": "mp4",
        "outtmpl": str(job_dir / "%(id)s.%(ext)s"),
    }
 
    def _run_download() -> Path:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = Path(ydl.prepare_filename(info))
            merged = filename.with_suffix(".mp4")  # merge can change the extension
            return merged if merged.exists() else filename
 
    try:
        file_path = await asyncio.to_thread(_run_download)
    except yt_dlp.utils.DownloadError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Download failed: {exc}") from exc
 
    if not file_path.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Download finished but the file wasn't found.")
 
    return FileResponse(
        path=file_path,
        media_type="video/mp4",
        filename=file_path.name,
        background=BackgroundTask(shutil.rmtree, job_dir, ignore_errors=True),
    )
 
 
@app.get("/healthz")
def healthz():
    return {"status": "ok"}
 