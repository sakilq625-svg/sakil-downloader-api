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

# Universal headers mimicking real devices
COMMON_HEADERS = {
    'User-Agent': 'com.google.android.youtube/19.09.37 (Linux; U; Android 14; US) gzip',
    'Accept-Language': 'en-US,en;q=0.9',
}

def get_ydl_options(download=False, outtmpl=None, format_selector=None, ext=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        # iOS, Android and TV clients bypass cloud server IP blocks without requiring login
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android", "web_creator"],
                "player_skip": ["webpage", "configs"]
            }
        },
        "http_headers": COMMON_HEADERS,
    }
    
    if not download:
        opts["skip_download"] = True
    else:
        opts["format"] = format_selector
        opts["outtmpl"] = outtmpl
        if ext and ext != "mp3":
            opts["merge_output_format"] = ext
            
    return opts

@app.get("/api/health")
def health_check():
    return {"status": "ok"}

@app.post("/api/fetch-info")
def fetch_video_info(req: VideoRequest):
    url_str = str(req.url)
    ydl_opts = get_ydl_options(download=False)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_str, download=False)
    except Exception as e:
        logger.error(f"First attempt failed: {str(e)}")
        # Fallback with mweb client if primary clients fail
        try:
            fallback_opts = {
                "quiet": True,
                "no_warnings": True,
                "nocheckcertificate": True,
                "skip_download": True,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["mweb"]
                    }
                }
            }
            with yt_dlp.YoutubeDL(fallback_opts) as ydl_fb:
                info = ydl_fb.extract_info(url_str, download=False)
        except Exception as err:
            logger.error(f"Fallback extraction failed: {str(err)}")
            raise HTTPException(status_code=400, detail="YouTube is currently restricting cloud server IPs for this video. Try another link or check again shortly.")

    formats_list = []
    seen_heights = set()

    # Generic or Direct stream format
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

    # Pure Audio option
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

    ydl_opts = get_ydl_options(
        download=True,
        outtmpl=temp_filepath,
        format_selector=format_selector,
        ext=ext
    )

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
