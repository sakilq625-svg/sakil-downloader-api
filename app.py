import os
import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Fast Social Video Downloader API")

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
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

@app.get("/api/health")
def health_check():
    return {"status": "ok"}

@app.post("/api/fetch-info")
def fetch_video_info(req: VideoRequest):
    url_str = str(req.url)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "http_headers": COMMON_HEADERS,
        "skip_download": True,
        "extract_flat": False,
    }

    # cookies.txt ফাইল থাকলে তা স্বয়ংক্রিয়ভাবে ব্যবহার করবে
    if os.path.exists("cookies.txt"):
        ydl_opts["cookiefile"] = "cookies.txt"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_str, download=False)
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.error(f"DownloadError: {error_msg}")
        if "429" in error_msg:
            raise HTTPException(
                status_code=429,
                detail="Instagram has rate-limited the server IP. Please try again later or configure authenticated cookies."
            )
        raise HTTPException(
            status_code=400,
            detail="Unable to fetch video. The link might be private, deleted, or region-restricted."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error processing video.")

    formats_list = []
    seen = set()
    raw_formats = info.get("formats", [])
    raw_formats.sort(key=lambda x: (x.get("height") or 0), reverse=True)

    for f in raw_formats:
        direct_url = f.get("url")
        if not direct_url:
            continue

        acodec = f.get("acodec", "none")
        vcodec = f.get("vcodec", "none")
        h = f.get("height")

        if vcodec != "none" and acodec != "none" and h:
            quality_tag = f"{h}p"
            if quality_tag not in seen:
                seen.add(quality_tag)
                formats_list.append({
                    "quality": quality_tag,
                    "label": f"{quality_tag} HD (MP4)",
                    "type": "video",
                    "filesize": f.get("filesize") or f.get("filesize_approx") or 0,
                    "download_url": direct_url
                })

    if not formats_list:
        fallback_url = info.get("url")
        if fallback_url:
            formats_list.append({
                "quality": "HD",
                "label": "HD Quality (MP4)",
                "type": "video",
                "filesize": info.get("filesize") or 0,
                "download_url": fallback_url
            })

    return {
        "title": info.get("title", "Social_Video"),
        "thumbnail": info.get("thumbnail") or "https://placehold.co/600x400/241C19/ffffff?text=Video+Ready",
        "duration": info.get("duration", 30),
        "uploader": info.get("uploader") or "Instagram / Facebook",
        "formats": formats_list
    }
