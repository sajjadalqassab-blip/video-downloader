import os
import uuid
import requests
import yt_dlp

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Google Drive
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Playwright
from playwright.sync_api import sync_playwright

# ============== CONFIG ==============
DRIVE_FOLDER_ID = "15slyKToMudp-SOHQx0FONS5r9HsXPE_3"

# Render stores secrets in /etc/secrets/...
CREDS_PATH = "/etc/secrets/GOOGLE_CREDS"

# ============== FASTAPI ==============
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class DownloadRequest(BaseModel):
    url: str
    filename: str | None = None

# ============== ALIEXPRESS EXTRACTOR ==============
def extract_aliexpress_video(url: str) -> str:
    print(f"[INFO] Extracting AliExpress video → {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
            )
        )

        page.goto(url, timeout=60000, wait_until="networkidle")

        # Scroll to load videos
        for _ in range(12):
            page.evaluate("window.scrollBy(0, 1000)")
            page.wait_for_timeout(400)

        video_sources = []

        # video source tags
        try:
            sources = page.query_selector_all("video source")
            for s in sources:
                src = s.get_attribute("src")
                if src and src.startswith("http"):
                    video_sources.append(src)
        except:
            pass

        # <video src="">
        try:
            videos = page.query_selector_all("video")
            for v in videos:
                src = v.get_attribute("src")
                if src and src.startswith("http"):
                    video_sources.append(src)
        except:
            pass

        browser.close()

        if video_sources:
            print(f"[OK] AliExpress video found: {video_sources[0]}")
            return video_sources[0]

        print("[WARN] No AliExpress video found")
        return None


# ============== YT-DLP DOWNLOADER ==============
def download_with_ytdlp(url: str) -> str:
    print(f"[INFO] Downloading via yt-dlp → {url}")

    outfile = f"{uuid.uuid4()}.mp4"

    # Clean indentation for ydl_opts
    ydl_opts = {
        "outtmpl": outfile,
        "format": "mp4",
        "cookiefile": "/etc/secrets/INSTAGRAM_COOKIES",
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
    }

    # ========== INSTAGRAM COOKIES SUPPORT ==========
    ig_cookies = os.getenv("IG_COOKIES")

    if "instagram.com" in url and ig_cookies:
        cookies_path = "/tmp/ig_cookies.txt"
        with open(cookies_path, "w") as f:
            f.write(ig_cookies)

        ydl_opts["cookiefile"] = cookies_path
        print("[INFO] Instagram cookies loaded for yt-dlp")
    # ===============================================

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print("[ERROR] yt-dlp failed:", str(e))
        return None

    print(f"[OK] yt-dlp download complete → {outfile}")
    return outfile

# ============== GOOGLE DRIVE UPLOAD ==============
def upload_to_drive(local_path, filename):
    creds = Credentials.from_service_account_file(
        CREDS_PATH,
        scopes=["https://www.googleapis.com/auth/drive"]
    )

    drive_service = build("drive", "v3", credentials=creds)

    metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID]
    }

    media = MediaFileUpload(local_path, mimetype="video/mp4", resumable=False)

    uploaded = drive_service.files().create(
        body=metadata,
        media_body=media,
        fields="id,webContentLink,webViewLink",
        supportsAllDrives=True
    ).execute()

    return uploaded


# ============== MAIN ENDPOINT ==============
@app.post("/download")
def download_video(request: DownloadRequest):
    url = request.url.strip()
    print(f"[INFO] Request → {url}")

    local_file = None

    # 1 — AliExpress
    if "aliexpress.com" in url:
        video_url = extract_aliexpress_video(url)
        if not video_url:
            raise HTTPException(status_code=500, detail="AliExpress video not found")

        filename = f"{uuid.uuid4()}.mp4"
        local_path = filename

        try:
            r = requests.get(video_url, stream=True, timeout=60)
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    f.write(chunk)
            local_file = local_path
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Download failed: {e}")

    else:
        # 2 — TikTok, Instagram, Facebook, etc.
        local_file = download_with_ytdlp(url)
        if not local_file:
            raise HTTPException(status_code=500, detail="yt-dlp failed")

    # Upload to Drive
    uploaded = upload_to_drive(local_file, os.path.basename(local_file))

    # Delete local file
    try:
        os.remove(local_file)
        print(f"[OK] Deleted local file")
    except:
        print("[WARN] Could not delete file")

    return {
        "success": True,
        "drive_file": uploaded
    }
