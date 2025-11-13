import os
import time
import json
import re
import requests
import yt_dlp
import psutil
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

# Parse creds
if not GOOGLE_CREDS_JSON:
    raise RuntimeError("❌ Missing GOOGLE_APPLICATION_CREDENTIALS_JSON environment variable")

try:
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(creds_dict)
    print("[OK] Loaded Google credentials from environment JSON ✅")
except Exception as e:
    raise RuntimeError(f"❌ Failed to parse credentials JSON: {e}")


# ─────────────────────────────────────────────
# Safe delete
# ─────────────────────────────────────────────
def safe_delete(path: str):
    for i in range(10):
        try:
            os.remove(path)
            print(f"[OK] Deleted local file: {path}")
            return
        except PermissionError:
            print(f"[WARN] File locked (attempt {i+1}/10)...")
            time.sleep(1)
    print(f"[FAIL] Could not delete: {path}")


# ─────────────────────────────────────────────
# Google Drive service
# ─────────────────────────────────────────────
def get_drive_service():
    creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")

    if creds_json:
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    raise HTTPException(status_code=500, detail="No credentials found")


# ─────────────────────────────────────────────
# Upload file to Drive
# ─────────────────────────────────────────────
def upload_to_drive(file_path: str, folder_id: str):
    service = get_drive_service()

    if not os.path.exists(file_path):
        raise HTTPException(status_code=400, detail="File not found")

    file_metadata = {"name": os.path.basename(file_path), "parents": [folder_id]}
    media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True)

    request_upload = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id,webViewLink,webContentLink",
        supportsAllDrives=True,
    )

    response = None
    while response is None:
        status, response = request_upload.next_chunk()
        if status:
            print(f"[UPLOAD] {int(status.progress() * 100)}%")

    file_id = response["id"]
    print(f"[OK] Uploaded → {file_id}")

    if MAKE_FILE_PUBLIC:
        service.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
            supportsAllDrives=True,
        ).execute()

    return response


# ─────────────────────────────────────────────
# AliExpress Video Extractor (FINAL WORKING VERSION)
# ─────────────────────────────────────────────
def download_tiktok_video(url: str, filename: str):
    save_path = os.path.join(os.getcwd(), f"{filename}.mp4")
    print(f"[INFO] Downloading {url}")

    # Strong headers for AliExpress
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": url,
    }

    ydl_opts = {
        "outtmpl": save_path,
        "format": "bestvideo+bestaudio/best",
        "quiet": False,
        "ignoreerrors": True,
        "merge_output_format": "mp4",
        "noprogress": True,
        "http_headers": headers,
        "force_generic_extractor": False,
    }

    # Detect AliExpress specifically
    if "aliexpress." in url:
        print("[INFO] Using yt-dlp AliExpress extractor...")
        ydl_opts["extractor_args"] = {
            "aliexpress": {
                "language": ["en_US"],
                "currency": ["USD"]
            }
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.download([url])

        time.sleep(2)

        if not os.path.exists(save_path):
            raise HTTPException(status_code=500, detail="Download failed: file not created")

        print(f"[OK] Download complete → {save_path}")
        return save_path

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")


# ─────────────────────────────────────────────
# DOWNLOAD Video (TikTok, Instagram, AliExpress)
# ─────────────────────────────────────────────
def download_tiktok_video(url: str, filename: str):
    save_path = os.path.join(os.getcwd(), f"{filename}.mp4")
    print(f"[INFO] Downloading {url}")

    # Handle AliExpress
    if "aliexpress.com" in url or "aliexpress.us" in url:
        ae_video = extract_aliexpress_video(url)
        if not ae_video:
            raise HTTPException(status_code=500, detail="AliExpress: No downloadable video found")
        url = ae_video

    # Normal downloader
    ydl_opts = {
        "outtmpl": save_path,
        "format": "best",
        "quiet": False,
        "noprogress": True,
        "merge_output_format": "mp4",
        "source_address": "0.0.0.0",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115 Safari/537.36"
            )
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if not os.path.exists(save_path):
            raise HTTPException(status_code=500, detail="Download failed")

        print(f"[OK] Download complete → {save_path}")
        return save_path

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {e}")


# ─────────────────────────────────────────────
# API Route
# ─────────────────────────────────────────────
@app.post("/download")
def download_and_upload(request: dict):
    url = request.get("url")
    filename = request.get("filename", "video")

    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url'")

    local_file = download_tiktok_video(url, filename)

    response = upload_to_drive(local_file, DRIVE_FOLDER_ID)
    safe_delete(local_file)
    return {"status": "success", "drive_response": response}


# ─────────────────────────────────────────────
# Error Handler
# ─────────────────────────────────────────────
@app.exception_handler(Exception)
async def exception_handler(request: Request, exc: Exception):
    print(f"[ERROR] {exc}")
    return JSONResponse(status_code=500, content={"detail": str(exc)})
