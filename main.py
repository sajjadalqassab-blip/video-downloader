import os
import time
import json
import requests
import psutil
import yt_dlp
import re
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

app = FastAPI(title="Video Downloader + Google Drive Uploader")

print(f"[DEBUG] DRIVE_FOLDER_ID   = {DRIVE_FOLDER_ID}")
print(f"[DEBUG] MAKE_FILE_PUBLIC  = {MAKE_FILE_PUBLIC}")

# ─────────────────────────────────────────────
# Google credentials
# ─────────────────────────────────────────────
if not GOOGLE_CREDS_JSON:
    raise RuntimeError("❌ Missing GOOGLE_APPLICATION_CREDENTIALS_JSON")

try:
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(creds_dict)
    print("[OK] Loaded Google credentials from environment JSON")
except Exception as e:
    raise RuntimeError(f"❌ Failed to parse Google credentials JSON: {e}")

# ─────────────────────────────────────────────
# Safe delete for Windows/Linux
# ─────────────────────────────────────────────
def safe_delete(path: str):
    for i in range(10):
        try:
            os.remove(path)
            print(f"[OK] Deleted local file: {path}")
            return
        except PermissionError:
            print(f"[WARN] File in use (attempt {i+1}/10)...")
            time.sleep(1)
        except Exception:
            pass
    print(f"[FAIL] Could not delete file: {path}")

# ─────────────────────────────────────────────
# Google Drive service
# ─────────────────────────────────────────────
def get_drive_service():
    info = json.loads(GOOGLE_CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

# ─────────────────────────────────────────────
# Upload file to Google Drive
# ─────────────────────────────────────────────
def upload_to_drive(file_path: str, folder_id: str):
    service = get_drive_service()

    if not os.path.exists(file_path):
        raise HTTPException(status_code=400, detail="File not found")

    # Validate folder
    try:
        folder = service.files().get(
            fileId=folder_id,
            fields="id,name",
            supportsAllDrives=True
        ).execute()
        print(f"[OK] Uploading into folder: {folder['name']}")
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Cannot access target folder: {e}")

    file_metadata = {"name": os.path.basename(file_path), "parents": [folder_id]}
    media = MediaFileUpload(file_path, resumable=True, mimetype="video/mp4")

    for attempt in range(1, 6):
        try:
            print(f"[INFO] Upload attempt {attempt}/5")

            request = service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id, webViewLink, webContentLink",
                supportsAllDrives=True,
            )

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    print(f"[UPLOAD] {int(status.progress() * 100)}%")

            print("[OK] Upload completed")

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

    raise HTTPException(status_code=502, detail="Upload failed after 5 retries")

# ─────────────────────────────────────────────
# AliExpress / Instagram / TikTok downloader
# ─────────────────────────────────────────────
def download_video(url: str, filename: str):
    save_path = os.path.join(os.getcwd(), f"{filename}.mp4")
    print(f"[INFO] Downloading {url}")

    is_ae = "aliexpress.com" in url or "aliexpress.us" in url

    # Strong browser headers
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.aliexpress.com/",
    }

    # Region override cookies (force Global site)
    cookies = {
        "aep_usuc_f": "site=glo&region=SA&b_locale=en_US"
    }

    # yt-dlp configuration
    ydl_opts = {
        "outtmpl": save_path,
        "quiet": False,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "source_address": "0.0.0.0",
        "http_headers": headers,
        "ignoreerrors": True,

        # AliExpress extractor patch
        "extractor_args": {
            "generic": {
                "force_generic_extractor": ["True"],
            },
            "aliexpress": {
                "use_webpage_url": ["True"],
                "retries": ["5"],
            }
        },

        "geo_bypass": True,
        "geo_bypass_country": "CN",
    }

    # Allow reading cookies from browser
    if is_ae:
        ydl_opts["cookiesfrombrowser"] = ("chrome",)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if not os.path.exists(save_path):
            raise HTTPException(status_code=500, detail="Download failed: No output file created")

        print(f"[OK] Download complete → {save_path}")
        return save_path

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download error: {e}")

# ─────────────────────────────────────────────
# API route
# ─────────────────────────────────────────────
@app.post("/download")
def download_and_upload(request: dict):
    url = request.get("url")
    filename = request.get("filename", "video")

    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url'")

    local_file = download_video(url, filename)

    try:
        result = upload_to_drive(local_file, DRIVE_FOLDER_ID)
        safe_delete(local_file)
        return {"status": "success", "drive": result}
    except Exception as e:
        safe_delete(local_file)
        raise e

# ─────────────────────────────────────────────
# Global error handler
# ─────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print(f"[ERROR] {exc}")
    return JSONResponse(status_code=500, content={"detail": str(exc)})
