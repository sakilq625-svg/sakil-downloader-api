from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
import yt_dlp
import logging
import urllib.parse
import os
import tempfile

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Video Downloader API")

# Allow requests from your GitHub Pages website
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class VideoRequest(BaseModel):
    url: HttpUrl

@app.get("/api/health")
def health_check():
    return {"status": "ok"}

@app.post("/api/fetch-info")
def fetch_video_info(req: VideoRequest):
    url_str = str(req.url)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_str, download=False)
    except Exception as e:
        logger.error(f"Extraction failed: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid link or the video is private.")

    formats_list = []
    seen_heights = set()

    if info.get("url") and not info.get("formats"):
        formats_list.append({
            "format_id": "best",
            "type": "video",
            "quality": "HD Standard",
            "label": "Full Video (MP4 + Audio)",
            "has_sound": True
        })

    raw_formats = info.get("formats", [])
    raw_formats.sort(key=lambda x: (x.get("height") or 0), reverse=True)

    for f in raw_formats:
        height = f.get("height")
        vcodec = f.get("vcodec", "none")

        if vcodec != "none" and height and height >= 144:
            if height not in seen_heights:
                seen_heights.add(height)
                if height >= 2160: quality_text = f"{height}p (4K Ultra HD)"
                elif height >= 1440: quality_text = f"{height}p (2K Quad HD)"
                elif height >= 1080: quality_text = f"{height}p (Full HD 1080p)"
                elif height >= 720: quality_text = f"{height}p (HD 720p)"
                elif height >= 480: quality_text = f"{height}p (SD 480p)"
                elif height >= 360: quality_text = f"{height}p (Medium 360p)"
                else: quality_text = f"{height}p (Low {height}p)"

                formats_list.append({
                    "format_id": str(f.get("format_id")),
                    "height": height,
                    "type": "video",
                    "quality": quality_text,
                    "label": f"{quality_text} • Audio Included 🔊",
                    "has_sound": True
                })

    formats_list.append({
        "format_id": "bestaudio",
        "type": "audio",
        "quality": "Audio Only",
        "label": "Audio Only (MP3 ~320kbps 🎵)",
        "has_sound": True
    })

    return {
        "title": info.get("title", "downloaded_video"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration", 0),
        "uploader": info.get("uploader", "Unknown"),
        "formats": formats_list,
        "original_url": url_str
    }

@app.get("/api/download")
def download_media(video_url: str = Query(...), format_id: str = Query(...), title: str = Query("video"), type: str = Query("video")):
    safe_title = "".join(c for c in title if c.isalnum() or c in " ._-").strip() or "video"
    ext = "mp3" if type == "audio" else "mp4"

    temp_dir = tempfile.mkdtemp()
    temp_filepath = os.path.join(temp_dir, f"%(title)s.%(ext)s")

    if type == "audio":
        format_selector = "bestaudio/best"
    elif format_id == "best":
        format_selector = "best"
    else:
        format_selector = f"{format_id}+bestaudio/bestvideo+bestaudio/best"

    ydl_opts = {
        "format": format_selector,
        "outtmpl": temp_filepath,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": ext if type != "audio" else None,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            download_info = ydl.extract_info(video_url, download=True)
            real_filepath = ydl.prepare_filename(download_info)
            
            base, _ = os.path.splitext(real_filepath)
            if os.path.exists(f"{base}.{ext}"):
                real_filepath = f"{base}.{ext}"
            elif not os.path.exists(real_filepath):
                files = os.listdir(temp_dir)
                if files:
                    real_filepath = os.path.join(temp_dir, files[0])
                else:
                    raise Exception("File not found on server.")
    except Exception as e:
        logger.error(f"Download processing error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to merge audio and video streams.")

    def file_stream_and_cleanup():
        try:
            with open(real_filepath, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        finally:
            if os.path.exists(real_filepath):
                os.remove(real_filepath)
            if os.path.exists(temp_dir):
                try:
                    os.rmdir(temp_dir)
                except Exception:
                    pass

    encoded_filename = urllib.parse.quote(f"{safe_title}.{ext}")
    headers = {
        "Content-Disposition": f"attachment; filename=\"{encoded_filename}\"; filename*=UTF-8''{encoded_filename}"
    }
    return StreamingResponse(file_stream_and_cleanup(), media_type="application/octet-stream", headers=headers)
