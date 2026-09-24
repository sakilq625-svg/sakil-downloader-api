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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class VideoRequest(BaseModel):
    url: HttpUrl

COMMON_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

def get_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "http_headers": COMMON_HEADERS,
        "extractor_args": {
            "youtube": {
                "player_client": ["tvhtml5_simply_embedded", "web_embedded", "android"]
            }
        }
    }

@app.get("/api/health")
def health_check():
    return {"status": "ok"}

@app.post("/api/fetch-info")
def fetch_video_info(req: VideoRequest):
    url_str = str(req.url)
    ydl_opts = get_opts()
    ydl_opts["skip_download"] = True

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_str, download=False)
    except Exception as e:
        logger.error(f"Extraction error: {str(e)}")
        raise HTTPException(status_code=400, detail="Cannot access media. The link may be private or restricted.")

    formats_list = []
    seen = set()

    raw_formats = info.get("formats", [])
    raw_formats.sort(key=lambda x: (x.get("height") or 0), reverse=True)

    for f in raw_formats:
        h = f.get("height")
        vcodec = f.get("vcodec", "none")
        if vcodec != "none" and h and h >= 360:
            if h not in seen:
                seen.add(h)
                formats_list.append({
                    "format_id": str(f.get("format_id")),
                    "height": h,
                    "type": "video",
                    "quality": f"{h}p",
                    "label": f"{h}p HD Video (MP4) 🔊",
                    "filesize": f.get("filesize") or f.get("filesize_approx") or 0
                })

    # Always ensure 720p or Best exists
    if not formats_list:
        formats_list.append({
            "format_id": "best",
            "height": 720,
            "type": "video",
            "quality": "720p",
            "label": "HD Quality (MP4) 🔊",
            "filesize": 0
        })

    # Audio track
    formats_list.append({
        "format_id": "bestaudio",
        "type": "audio",
        "quality": "Audio",
        "label": "High Quality Audio (MP3 🎵)",
        "filesize": 0
    })

    return {
        "title": info.get("title", "video"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration", 180),
        "uploader": info.get("uploader", "Social Media"),
        "formats": formats_list,
        "original_url": url_str
    }

@app.get("/api/download")
def download_media(video_url: str = Query(...), format_id: str = Query(...), title: str = Query("video"), type: str = Query("video")):
    safe_title = "".join(c for c in title if c.isalnum() or c in " ._-").strip() or "video"
    ext = "mp3" if type == "audio" else "mp4"

    temp_dir = tempfile.mkdtemp()
    temp_filepath = os.path.join(temp_dir, "%(title)s.%(ext)s")

    if type == "audio":
        format_selector = "bestaudio/best"
    elif format_id == "best":
        format_selector = "bestvideo+bestaudio/best"
    else:
        format_selector = f"{format_id}+bestaudio/bestvideo+bestaudio/best"

    ydl_opts = get_opts()
    ydl_opts.update({
        "format": format_selector,
        "outtmpl": temp_filepath,
        "merge_output_format": ext if type != "audio" else None,
    })

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
        logger.error(f"Download error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to process and merge media streams.")

    def stream_and_clean():
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

    encoded_name = urllib.parse.quote(f"{safe_title}.{ext}")
    headers = {
        "Content-Disposition": f"attachment; filename=\"{encoded_name}\"; filename*=UTF-8''{encoded_name}"
    }
    return StreamingResponse(stream_and_clean(), media_type="application/octet-stream", headers=headers)
