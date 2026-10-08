from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
import requests
import re
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Fast Instagram & Facebook Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class VideoRequest(BaseModel):
    url: HttpUrl

# Official Instagram Web App Headers (Bypasses anonymous datacenter 429 blocking)
IG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "X-IG-App-ID": "936619743392459",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin"
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

    # ==========================================
    # 1. INSTAGRAM FAST PARSER (3-TIER ENGINE)
    # ==========================================
    if "instagram.com" in url_str:
        shortcode = extract_shortcode(url_str)
        if not shortcode:
            raise HTTPException(status_code=400, detail="Invalid Instagram URL format.")

        clean_video_url = None
        clean_thumb = None

        # Method 1: Official Instagram App API Endpoint
        try:
            api_url = f"https://www.instagram.com/api/v1/oembed/?url=https://www.instagram.com/reel/{shortcode}/"
            res = requests.get(api_url, headers=IG_HEADERS, timeout=8)
            if res.status_code == 200:
                data = res.json()
                clean_thumb = data.get("thumbnail_url")
        except Exception as e:
            logger.warning(f"OEmbed failed: {e}")

        # Method 2: Public Mobile Ajax Extractor (Direct MP4 CDN)
        try:
            ajax_url = f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis"
            res_ajax = requests.get(ajax_url, headers=IG_HEADERS, timeout=8)
            if res_ajax.status_code == 200:
                ajax_json = res_ajax.json()
                items = ajax_json.get("items", [])
                if items:
                    item = items[0]
                    video_versions = item.get("video_versions", [])
                    if video_versions:
                        clean_video_url = video_versions[0].get("url")
                    if not clean_thumb and item.get("image_versions2", {}).get("candidates"):
                        clean_thumb = item["image_versions2"]["candidates"][0].get("url")
        except Exception as e:
            logger.warning(f"Ajax method failed: {e}")

        # Method 3: Reverse Web Embed Token Fallback
        if not clean_video_url:
            try:
                embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
                res_embed = requests.get(embed_url, headers=IG_HEADERS, timeout=8)
                if res_embed.status_code == 200:
                    html = res_embed.text
                    v_match = re.search(r'\"video_url\":\"([^\"]+)\"', html)
                    t_match = re.search(r'\"display_url\":\"([^\"]+)\"', html)
                    if v_match:
                        clean_video_url = v_match.group(1).replace("\\u0026", "&").replace("&amp;", "&")
                    if t_match and not clean_thumb:
                        clean_thumb = t_match.group(1).replace("\\u0026", "&").replace("&amp;", "&")
            except Exception as e:
                logger.warning(f"Embed fallback failed: {e}")

        if clean_video_url:
            return {
                "title": f"Instagram_Reel_{shortcode}",
                "thumbnail": clean_thumb or "https://placehold.co/600x400/241C19/ffffff?text=Instagram+Reel",
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

    # ==========================================
    # 2. FACEBOOK & UNIVERSAL FALLBACK ENGINE
    # ==========================================
    try:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
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

            if formats:
                return {
                    "title": info.get("title", "Social_Video"),
                    "thumbnail": info.get("thumbnail") or "https://placehold.co/600x400/241C19/ffffff?text=Video+Ready",
                    "duration": info.get("duration", 30),
                    "uploader": info.get("uploader", "Social Media"),
                    "formats": formats
                }
    except Exception as e:
        logger.error(f"Fallback extractor error: {str(e)}")

    raise HTTPException(
        status_code=400,
        detail="Cannot fetch video. Please ensure the link is public and accessible without login."
    )
