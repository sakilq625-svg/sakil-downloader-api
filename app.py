from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urlparse, quote
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata

import requests
import yt_dlp

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

APP_VERSION = "ig-fb-v3"

app = FastAPI(title="Instagram & Facebook Video Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class VideoRequest(BaseModel):
    url: HttpUrl


# Only these sites are accepted. This keeps the server from being used as
# a general-purpose downloader / proxy by strangers.
ALLOWED_HOSTS = ("instagram.com", "instagr.am", "facebook.com", "fb.watch", "fb.com")

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Optional: a Render Secret File named cookies.txt. Instagram/Facebook may
# demand a login for requests coming from cloud servers; if the file
# exists it is used, if not everything still works for public links.
COOKIE_PATH_CANDIDATES = [
    os.environ.get("COOKIES_PATH", ""),
    "/etc/secrets/cookies.txt",
]
_cookie_copy_path = None

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def is_allowed_url(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def get_cookie_file():
    """Returns a writable copy of the cookie file (yt-dlp rewrites it on
    exit and Render secret files are read-only), or None."""
    global _cookie_copy_path
    if _cookie_copy_path and os.path.exists(_cookie_copy_path):
        return _cookie_copy_path
    for src in COOKIE_PATH_CANDIDATES:
        if src and os.path.isfile(src):
            try:
                dst = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
                shutil.copyfile(src, dst)
                _cookie_copy_path = dst
                return dst
            except OSError as e:
                logger.warning(f"Could not copy cookie file: {e}")
    return None


def clean_error(e):
    msg = ANSI_RE.sub("", str(e)).strip().split("\n")[0]
    return msg.replace("ERROR: ", "")[:220]


def friendly_error(e):
    raw = clean_error(e)
    low = raw.lower()
    blocked_words = ("login", "log in", "cookie", "empty media response",
                     "rate-limit", "rate limit", "private", "not available")
    if any(w in low for w in blocked_words):
        msg = ("Instagram/Facebook is not giving anonymous access to this link "
               "(private post, login wall, or rate limit).")
    else:
        msg = ("Could not read this video. Make sure it is a public Instagram "
               "Reel/post or Facebook video link.")
    return f"{msg} Details: {raw} (server {APP_VERSION})"


def safe_filename(title):
    keep = []
    for c in title or "":
        if unicodedata.category(c)[0] in ("L", "M", "N") or c in " ._-":
            keep.append(c)
    name = re.sub(r"\s+", " ", "".join(keep)).strip(" ._-")[:80]
    return name or "video"


def content_disposition(filename):
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", filename) or "video.mp4"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def get_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": True,
        "http_headers": COMMON_HEADERS,
        "noplaylist": True,      # only the single reel/post that was linked
        "socket_timeout": 15,    # fail fast instead of hanging
        "retries": 2,
        "cachedir": False,
    }
    cookie_file = get_cookie_file()
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts


# ----------------------------------------------------------------------
# Short-lived cache: fetch-info stores the direct CDN link of every
# quality, so the download that follows starts instantly instead of
# running a second extraction.
# ----------------------------------------------------------------------
CACHE_TTL = 20 * 60
CACHE_MAX = 200
_cache = {}
_cache_lock = threading.Lock()


def cache_key(url):
    """Same key for the link however it was typed (trailing '/', host case)."""
    p = urlparse((url or "").strip())
    return f"{(p.hostname or '').lower()}{p.path.rstrip('/')}?{p.query}"


def cache_put(video_url, entries):
    video_url = cache_key(video_url)
    now = time.time()
    with _cache_lock:
        for k in [k for k, (exp, _) in _cache.items() if exp < now]:
            del _cache[k]
        while len(_cache) >= CACHE_MAX:
            del _cache[min(_cache, key=lambda k: _cache[k][0])]
        _cache[video_url] = (now + CACHE_TTL, entries)


def cache_get(video_url, format_id):
    video_url = cache_key(video_url)
    with _cache_lock:
        item = _cache.get(video_url)
        if not item:
            return None
        exp, entries = item
        if exp < time.time():
            _cache.pop(video_url, None)
            return None
        return entries.get(format_id)


def cache_drop(video_url):
    video_url = cache_key(video_url)
    with _cache_lock:
        _cache.pop(video_url, None)


# ----------------------------------------------------------------------
# Extraction + format building
# ----------------------------------------------------------------------
def extract_info(url):
    opts = get_opts()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return first_entry(info)


def first_entry(info):
    """Instagram carousels come back as a playlist: use the first item."""
    if info and info.get("_type") == "playlist":
        for entry in info.get("entries") or []:
            if entry:
                return entry
    return info


def has_video(f):
    return f.get("vcodec") != "none"      # None = unknown, treated as video


def has_audio(f):
    return f.get("acodec") != "none"      # None = unknown, treated as audio


def is_direct_http(f):
    return bool(f.get("url")) and str(f.get("protocol") or "https") in ("http", "https")


def quality_of(f):
    """Short side of the frame: 720x1280 (portrait reel) and 1280x720 are both 720p."""
    w, h = f.get("width"), f.get("height")
    if w and h:
        return min(w, h)
    return h or w or None


def format_size(f):
    return f.get("filesize") or f.get("filesize_approx") or 0


def probe_size(url, headers):
    """Real size from the CDN itself (Content-Range / Content-Length)."""
    h = dict(headers or {})
    h["Range"] = "bytes=0-0"
    h["Accept-Encoding"] = "identity"
    try:
        r = requests.get(url, headers=h, stream=True, timeout=(3, 4), allow_redirects=True)
        try:
            m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
            if m:
                return int(m.group(1))
            cl = r.headers.get("Content-Length", "")
            if r.status_code == 200 and cl.isdigit():
                return int(cl)
        finally:
            r.close()
    except Exception as e:
        logger.info(f"size probe failed: {e}")
    return 0


def probe_missing_sizes(jobs):
    """jobs: {key: (url, headers)} -> {key: size}. Runs in parallel, ~6s budget."""
    if not jobs:
        return {}
    pool = ThreadPoolExecutor(max_workers=6)
    futures = {key: pool.submit(probe_size, url, headers) for key, (url, headers) in jobs.items()}
    wait(list(futures.values()), timeout=6)
    pool.shutdown(wait=False)
    result = {}
    for key, fut in futures.items():
        try:
            result[key] = fut.result(timeout=0) if fut.done() else 0
        except Exception:
            result[key] = 0
    return result


def build_formats(info, probe=True):
    """Returns (public_formats, cache_entries)."""
    info = info or {}
    raw = [f for f in (info.get("formats") or []) if f.get("format_id") is not None]

    if not raw and info.get("url"):     # single direct link, no format list
        raw = [{
            "format_id": "direct", "url": info["url"], "protocol": "https",
            "width": info.get("width"), "height": info.get("height"),
            "http_headers": info.get("http_headers"),
        }]

    videos = [f for f in raw if has_video(f) and f.get("url")]
    audios = [f for f in raw if not has_video(f) and has_audio(f) and f.get("url")]
    best_audio = max(audios, key=lambda f: f.get("abr") or f.get("tbr") or 0) if audios else None

    videos.sort(key=lambda f: (quality_of(f) or 0, f.get("tbr") or 0, format_size(f)), reverse=True)

    # One format per quality. Preference: a direct video+audio file (streamed
    # straight through, fastest) > any other video+audio format > video-only
    # that has to be merged with the best audio on the server.
    chosen = {}
    for f in videos:
        muxed = has_audio(f)
        progressive = muxed and is_direct_http(f)
        if not muxed and best_audio is None:
            continue                     # would be a silent video
        q = quality_of(f)
        if q is not None and q < 240:
            continue
        rank = 2 if progressive else (1 if muxed else 0)
        current = chosen.get(q)
        if current is None or rank > current[1]:
            chosen[q] = (f, rank)

    ordered = sorted(chosen.items(), key=lambda kv: kv[0] or 0, reverse=True)[:4]

    # Real sizes: from yt-dlp if known, otherwise ask the CDN directly.
    jobs = {}
    if probe:
        for q, (f, rank) in ordered:
            if not format_size(f) and is_direct_http(f):
                jobs[("v", f["format_id"])] = (f["url"], f.get("http_headers") or info.get("http_headers"))
        needs_audio = any(rank == 0 for _, (_, rank) in ordered)
        if (best_audio is not None and needs_audio
                and not format_size(best_audio) and is_direct_http(best_audio)):
            jobs[("a", best_audio["format_id"])] = (best_audio["url"], best_audio.get("http_headers"))
    probed = probe_missing_sizes(jobs)

    audio_size = 0
    if best_audio is not None:
        audio_size = format_size(best_audio) or probed.get(("a", best_audio["format_id"]), 0)

    public, entries = [], {}
    for q, (f, rank) in ordered:
        fid = str(f["format_id"])
        muxed = rank >= 1
        progressive = rank == 2

        size = format_size(f) or probed.get(("v", f["format_id"]), 0)
        if size and not muxed:
            size = size + audio_size if audio_size else 0     # unknown audio -> unknown total

        headers = {**COMMON_HEADERS, **(f.get("http_headers") or info.get("http_headers") or {})}
        entries[fid] = {
            "url": f.get("url"),
            "headers": headers,
            "progressive": progressive,
            "muxed": muxed,
        }

        label = "HD Video (MP4)" if q is None else (f"{q}p HD Video (MP4)" if q >= 720 else f"{q}p Video (MP4)")
        public.append({
            "format_id": fid,
            "type": "video",
            "quality": "HD" if q is None else f"{q}p",
            "height": q,
            "label": label,
            "filesize": int(size or 0),
            "merge": not progressive,
        })

    if public:
        duration = info.get("duration") or 0
        public.append({
            "format_id": "bestaudio",
            "type": "audio",
            "quality": "Audio",
            "label": "Audio Only (MP3)",
            "filesize": int(duration * 24000) if duration else 0,   # 192 kbit/s
            "merge": False,
        })

    return public, entries


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.get("/")
def root():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "version": APP_VERSION,
        "cookies_loaded": get_cookie_file() is not None,
    }


@app.post("/api/fetch-info")
def fetch_video_info(req: VideoRequest):
    url = str(req.url)
    if not is_allowed_url(url):
        raise HTTPException(status_code=400, detail="Only Instagram and Facebook video links are supported.")

    try:
        info = extract_info(url)
    except Exception as e:
        logger.error(f"Extraction error: {clean_error(e)}")
        raise HTTPException(status_code=400, detail=friendly_error(e))

    public, entries = build_formats(info, probe=True)
    if not public:
        raise HTTPException(
            status_code=400,
            detail=f"No downloadable video was found at this link. (server {APP_VERSION})"
        )

    cache_put(url, entries)

    return {
        "title": (info.get("title") or "video")[:150],
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration") or 0,
        "uploader": info.get("uploader") or info.get("channel") or "Social Media",
        "formats": public,
        "version": APP_VERSION,
    }


def refresh_entry(video_url, format_id):
    """Extract again (no size probing, for speed) and return the entry."""
    try:
        info = extract_info(video_url)
        _, entries = build_formats(info, probe=False)
    except Exception as e:
        logger.warning(f"Refresh failed: {clean_error(e)}")
        return None
    if entries:
        cache_put(video_url, entries)
    return entries.get(format_id)


def open_upstream(entry):
    headers = dict(entry.get("headers") or {})
    headers["Accept-Encoding"] = "identity"
    try:
        r = requests.get(entry["url"], headers=headers, stream=True, timeout=(5, 30), allow_redirects=True)
    except Exception as e:
        logger.warning(f"Upstream connect failed: {e}")
        return None
    if r.status_code >= 400:
        logger.warning(f"Upstream returned {r.status_code}")
        r.close()
        return None
    return r


def stream_upstream(resp, name):
    def gen():
        try:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    headers = {"Content-Disposition": content_disposition(f"{name}.mp4")}
    length = resp.headers.get("Content-Length", "")
    if length.isdigit() and not resp.headers.get("Content-Encoding"):
        headers["Content-Length"] = length
    return StreamingResponse(gen(), media_type="video/mp4", headers=headers)


def run_ytdlp_download(video_url, selector, audio_mp3=False):
    """Downloads (and merges / converts) into a temp dir. Returns (dir, file)."""
    temp_dir = tempfile.mkdtemp()
    opts = get_opts()
    opts.update({
        "format": selector,
        "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
    })
    if audio_mp3:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(video_url, download=True)
        files = [f for f in os.listdir(temp_dir) if not f.endswith((".part", ".ytdl", ".temp"))]
        if not files:
            raise RuntimeError("No output file was produced.")
        wanted = ".mp3" if audio_mp3 else ".mp4"
        files.sort(key=lambda f: (f.endswith(wanted), os.path.getsize(os.path.join(temp_dir, f))), reverse=True)
        return temp_dir, os.path.join(temp_dir, files[0])
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def stream_file_and_clean(temp_dir, filepath, name):
    ext = os.path.splitext(filepath)[1] or ".mp4"

    def gen():
        try:
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    media_type = "audio/mpeg" if ext == ".mp3" else "video/mp4"
    headers = {
        "Content-Disposition": content_disposition(f"{name}{ext}"),
        "Content-Length": str(os.path.getsize(filepath)),
    }
    return StreamingResponse(gen(), media_type=media_type, headers=headers)


@app.get("/api/download")
def download_media(
    video_url: str = Query(...),
    format_id: str = Query(...),
    title: str = Query("video"),
    type: str = Query("video"),
):
    if not is_allowed_url(video_url):
        raise HTTPException(status_code=400, detail="Only Instagram and Facebook video links are supported.")

    name = safe_filename(title)

    try:
        # ---- Audio (MP3) ------------------------------------------------
        if type == "audio":
            temp_dir, filepath = run_ytdlp_download(video_url, "bestaudio/best", audio_mp3=True)
            return stream_file_and_clean(temp_dir, filepath, name)

        # ---- Video: fast path = stream the CDN file straight through -------
        entry = cache_get(video_url, format_id) or refresh_entry(video_url, format_id)

        if entry and entry.get("progressive"):
            resp = open_upstream(entry)
            if resp is None:                       # link expired: refresh once
                cache_drop(video_url)
                entry = refresh_entry(video_url, format_id)
                resp = open_upstream(entry) if entry and entry.get("progressive") else None
            if resp is not None:
                return stream_upstream(resp, name)

        # ---- Video: slow path = download + merge on the server -------------
        if entry and entry.get("muxed"):
            selector = f"{format_id}/best"            # already has audio
        else:
            selector = f"{format_id}+bestaudio/bestvideo+bestaudio/best"
        temp_dir, filepath = run_ytdlp_download(video_url, selector)
        return stream_file_and_clean(temp_dir, filepath, name)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Download error: {clean_error(e)}")
        raise HTTPException(status_code=500, detail=f"Download failed: {clean_error(e)} (server {APP_VERSION})")
