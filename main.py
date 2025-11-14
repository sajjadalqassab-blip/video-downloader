import os
import time
import json
import requests
import re
import psutil
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2 import service_account
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# Load environment variables
# ─────────────────────────────────────────────
load_dotenv()

GOOGLE_CREDS_JSON = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
MAKE_FILE_PUBLIC = os.getenv("MAKE_FILE_PUBLIC", "true").lower() == "true"

app = FastAPI(title="Video Downloader + Drive Uploader")

print(f"[DEBUG] DRIVE_FOLDER_ID = {DRIVE_FOLDER_ID}")

# ─────────────────────────────────────────────
# Google Credentials
# ─────────────────────────────────────────────
if not GOOGLE_CREDS_JSON:
    raise RuntimeError("Missing GOOGLE_APPLICATION_CREDENTIALS_JSON")

try:
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(creds_dict)
    print("[OK] Google credentials loaded")
except Exception as e:
    raise RuntimeError(f"Invalid Google credentials JSON: {e}")

# ─────────────────────────────────────────────
# Safe delete
# ─────────────────────────────────────────────
def safe_delete(path: str):
    for i in range(10):
        try:
            os.remove(path)
            print(f"[OK] Deleted {path}")
            return
        except Exception:
            time.sleep(1)
    print(f"[FAIL] Could not delete {path}")

# ─────────────────────────────────────────────
# Google Drive Service
# ─────────────────────────────────────────────
def get_drive_service():
    info = json.loads(GOOGLE_CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

# ─────────────────────────────────────────────
# Upload to Drive
# ─────────────────────────────────────────────
def upload_to_drive(file_path: str, folder_id: str):
    service = get_drive_service()

    if not os.path.exists(file_path):
        raise HTTPException(400, "File not found")

    # Validate folder
    try:
        folder = service.files().get(
            fileId=folder_id,
            fields="id,name",
            supportsAllDrives=True
        ).execute()
        print(f"[OK] Uploading to folder: {folder['name']}")
    except Exception as e:
        raise HTTPException(404, f"Cannot access folder: {e}")

    metadata = {"name": os.path.basename(file_path), "parents": [folder_id]}
    media = MediaFileUpload(file_path, resumable=True)

    for attempt in range(1, 6):
        try:
            print(f"[INFO] Upload attempt {attempt}/5")
            req = service.files().create(
                body=metadata,
                media_body=media,
                supportsAllDrives=True,
                fields="id,webViewLink,webContentLink"
            )
            response = None
            while response is None:
                status, response = req.next_chunk()
                if status:
                    print(f"[UPLOAD] {int(status.progress()*100)}%")
            print("[OK] Upload complete")

            if MAKE_FILE_PUBLIC:
                service.permissions().create(
                    fileId=response["id"],
                    body={"role": "reader", "type": "anyone"},
                    supportsAllDrives=True
                ).execute()

            return response

        except Exception as e:
            print(f"[WARN] Upload failed: {e}")
            time.sleep(attempt * 5)

    raise HTTPException(502, "Upload failed after 5 retries")

# ─────────────────────────────────────────────
# Download TikTok / IG / AliExpress
# ─────────────────────────────────────────────
def download_video(url: str, filename: str):
    save_path = os.path.join(os.getcwd(), f"{filename}.mp4")
    print(f"[INFO] Downloading → {url}")

    is_ae = ("aliexpress.com" in url) or ("aliexpress.us" in url)

    # Global headers for spoofing browser
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.aliexpress.com/",
    }

    # Region override cookies to bypass US redirect
    cookies = {"aep_usuc_f": "site=glo&region=SA&b_locale=en_US"}

    ydl_opts = {
        "outtmpl": save_path,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": False,
        "source_address": "0.0.0.0",
        "http_headers": headers,
        "geo_bypass": True,
        "geo_bypass_country": "CN",
        "ignoreerrors": True,

        # AliExpress hack
        "extractor_args": {
            "generic": {"force_generic_extractor": ["True"]},
            "aliexpress": {
                "use_webpage_url": ["True"],
                "retries": ["5"],
                "player_client": ["desktop"],
            },
        },
    }

    # ❌ Removed → ydl_opts["cookiesfrombrowser"] = ("chrome",)
    # Render has no Chrome

    if is_ae:
        print("[INFO] Using AliExpress extractor override")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if not os.path.exists(save_path):
            raise HTTPException(500, "Download failed: file missing")

        print(f"[OK] Saved → {save_path}")
        return save_path

    except Exception as e:
        raise HTTPException(500, f"Download failed: {e}")

# ─────────────────────────────────────────────
# API Route
# ─────────────────────────────────────────────
@app.post("/download")
def download_and_upload(request: dict):
    url = request.get("url")
    filename = request.get("filename", "video")

    if not url:
        raise HTTPException(400, "Missing 'url'")

    local_file = download_video(url, filename)

    try:
        drive_resp = upload_to_drive(local_file, DRIVE_FOLDER_ID)
        safe_delete(local_file)
        return {"status": "success", "drive": drive_resp}
    except Exception as e:
        safe_delete(local_file)
        raise e

# ─────────────────────────────────────────────
# Global Error Handler
# ─────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception(request: Request, exc: Exception):
    print(f"[ERROR] {exc}")
    return JSONResponse(status_code=500, content={"detail": str(exc)})
