from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
import requests
import re
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Social Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class VideoRequest(BaseModel):
    url: HttpUrl

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate"
}

def extract_shortcode(url: str):
    match = re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', url)
    return match.group(1) if match else None

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.post("/api/fetch-info")
def fetch_info(req: VideoRequest):
    url_str = str(req.url)
    
    # 1. Instagram Direct Embed Parser (Bypasses 429 Data Center Blocking)
    if "instagram.com" in url_str:
        shortcode = extract_shortcode(url_str)
        if not shortcode:
            raise HTTPException(status_code=400, detail="Invalid Instagram URL format.")
        
        embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
        try:
            res = requests.get(embed_url, headers=HEADERS, timeout=12)
            if res.status_code == 200:
                html = res.text
                
                # Extract clean video source
                video_url_match = re.search(r'\"video_url\":\"([^\"]+)\"', html)
                if not video_url_match:
                    video_url_match = re.search(r'<video[^>]+src=\"([^\"]+)\"', html)
                
                # Extract clean thumbnail
                thumb_match = re.search(r'\"display_url\":\"([^\"]+)\"', html)
                if not thumb_match:
                    thumb_match = re.search(r'<img[^>]+class=\"EmbeddedMediaImage\"[^>]+src=\"([^\"]+)\"', html)

                if video_url_match:
                    clean_video_url = video_url_match.group(1).replace("\\u0026", "&").replace("&amp;", "&")
                    clean_thumb = thumb_match.group(1).replace("\\u0026", "&").replace("&amp;", "&") if thumb_match else ""

                    return {
                        "title": f"Instagram_Reel_{shortcode}",
                        "thumbnail": clean_thumb or "https://placehold.co/600x400/241C19/ffffff?text=Instagram+Video",
                        "duration": 30,
                        "uploader": "Instagram",
                        "formats": [
                            {
                                "quality": "HD",
                                "label": "Original HD (MP4)",
                                "type": "video",
                                "filesize": 0,
                                "download_url": clean_video_url
                            }
                        ]
                    }
        except Exception as e:
            logger.error(f"Embed extraction failed: {str(e)}")

    # 2. Facebook & Secondary Fallback Processing
    try:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "http_headers": HEADERS
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_str, download=False)
            formats = []
            for f in info.get("formats", []):
                if f.get("url") and f.get("vcodec") != "none" and f.get("acodec") != "none":
                    formats.append({
                        "quality": f"{f.get('height', 'HD')}p",
                        "label": f"{f.get('height', 'HD')}p HD (MP4)",
                        "type": "video",
                        "filesize": f.get("filesize") or 0,
                        "download_url": f.get("url")
                    })
            if not formats and info.get("url"):
                formats.append({
                    "quality": "HD",
                    "label": "HD (MP4)",
                    "type": "video",
                    "filesize": 0,
                    "download_url": info.get("url")
                })
            
            return {
                "title": info.get("title", "Social_Video"),
                "thumbnail": info.get("thumbnail") or "https://placehold.co/600x400/241C19/ffffff?text=Video+Ready",
                "duration": info.get("duration", 30),
                "uploader": info.get("uploader", "Social Media"),
                "formats": formats
            }
    except Exception as e:
        logger.error(f"Fallback error: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail="Unable to access media. The post might be private, restricted, or unavailable."
        )
