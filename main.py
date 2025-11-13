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

# Get credentials JSON directly (Render env var)
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
MAKE_FILE_PUBLIC = os.getenv("MAKE_FILE_PUBLIC", "true").lower() == "true"

app = FastAPI(title="Video Downloader + Google Drive Uploader")

print(f"[DEBUG] DRIVE_FOLDER_ID   = {DRIVE_FOLDER_ID}")
print(f"[DEBUG] MAKE_FILE_PUBLIC  = {MAKE_FILE_PUBLIC}")

# Parse credentials from JSON env var
if not GOOGLE_CREDS_JSON:
    raise RuntimeError("❌ Missing GOOGLE_APPLICATION_CREDENTIALS_JSON environment variable")

try:
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(creds_dict)
    print("[OK] Loaded Google credentials from environment JSON ✅")
except Exception as e:
    raise RuntimeError(f"❌ Failed to parse credentials JSON: {e}")

# ─────────────────────────────────────────────
# Safe delete (handles Windows file locks)
# ─────────────────────────────────────────────
def safe_delete(path: str):
    for i in range(10):
        try:
            os.remove(path)
            print(f"[OK] Deleted local file: {path}")
            return
        except PermissionError:
            print(f"[WARN] File still in use (attempt {i+1}/10)...")
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    for f in proc.open_files():
                        if f.path == path:
                            print(f"⚠ Locked by process: {proc.name()} (PID {proc.pid})")
                except Exception:
                    pass
            time.sleep(1)
    print(f"[FAIL] Could not delete: {path}")

# ─────────────────────────────────────────────
# Google Drive auth
# ─────────────────────────────────────────────
def get_drive_service():
    creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

    if creds_json:
        try:
            info = json.loads(creds_json)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Invalid GOOGLE_APPLICATION_CREDENTIALS_JSON: {e}")
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    if not creds_path or not os.path.exists(creds_path):
        raise HTTPException(status_code=500, detail="No Drive credentials found.")

    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

# ─────────────────────────────────────────────
# Upload to Shared Drive / My Drive
# ─────────────────────────────────────────────
def upload_to_drive(file_path: str, folder_id: str):
    service = get_drive_service()

    if not os.path.exists(file_path):
        raise HTTPException(status_code=400, detail=f"File not found: {file_path}")

    # Verify folder access
    try:
        folder_meta = service.files().get(
            fileId=folder_id,
            fields="id,name,mimeType,driveId,trashed",
            supportsAllDrives=True,
        ).execute()
        if folder_meta.get("trashed"):
            raise HTTPException(status_code=400, detail="Target folder is in Trash")
        print(f"[OK] Target folder: {folder_meta['name']} (driveId={folder_meta.get('driveId')})")
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Cannot access folder {folder_id}: {e}")

    file_metadata = {"name": os.path.basename(file_path), "parents": [folder_id]}
    media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True)

    for attempt in range(1, 6):
        try:
            print(f"[INFO] Upload attempt {attempt}/5 → {file_path}")
            request = service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id,webViewLink,webContentLink",
                supportsAllDrives=True,
            )
            response = None
            last_progress = -1
            while response is None:
                status, response = request.next_chunk()
                if status:
                    p = int(status.progress() * 100)
                    if p != last_progress:
                        print(f"[UPLOAD] {p}%")
                        last_progress = p
            file_id = response["id"]
            print(f"[OK] Uploaded successfully → {file_id}")

            if MAKE_FILE_PUBLIC:
                service.permissions().create(
                    fileId=file_id,
                    body={"role": "reader", "type": "anyone"},
                    supportsAllDrives=True,
                ).execute()
                print("[OK] File made public.")

            return response
        except Exception as e:
            print(f"[WARN] Upload failed (attempt {attempt}/5): {e}")
            time.sleep(attempt * 10)

    raise HTTPException(status_code=502, detail="Drive upload failed after 5 attempts")

# ─────────────────────────────────────────────
# AliExpress video extractor
# ─────────────────────────────────────────────
def extract_aliexpress_video(url: str) -> str:
    print(f"[INFO] Trying AliExpress extractor → {url}")

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/115.0 Safari/537.36"
            )
        }

        html = requests.get(url, headers=headers, timeout=15).text
        matches = re.findall(r'"videoUrl":"(.*?)"', html)

        if matches:
            video_url = matches[0].replace("\\u002F", "/")
            print(f"[OK] AliExpress video found → {video_url}")
            return video_url

        print("[WARN] No video found in AliExpress page.")
        return None

    except Exception as e:
        print(f"[ERROR] AliExpress extractor error: {e}")
        return None

# ─────────────────────────────────────────────
# Download TikTok / Instagram / AliExpress
# ─────────────────────────────────────────────
def download_tiktok_video(url: str, filename: str):
    save_path = os.path.join(os.getcwd(), f"{filename}.mp4")
    print(f"[INFO] Downloading {url}")

    # AliExpress detection
    if "aliexpress.com" in url or "aliexpress.us" in url:
        ae_video = extract_aliexpress_video(url)
        if not ae_video:
            raise HTTPException(status_code=500, detail="AliExpress: No downloadable video found")
        url = ae_video  # override with direct CDN video

    ydl_opts = {
        "outtmpl": save_path,
        "format": "best",
        "quiet": False,
        "no_warnings": True,
        "ignoreerrors": True,
        "noprogress": True,
        "merge_output_format": "mp4",
        "source_address": "0.0.0.0",
        "force_generic_extractor": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/108.0 Safari/537.36"
            )
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        time.sleep(2)

        if not os.path.exists(save_path):
            raise HTTPException(status_code=500, detail="Download failed: file not created")

        print(f"[OK] Download complete → {save_path}")
        return save_path

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {e}")

# ─────────────────────────────────────────────
# API route
# ─────────────────────────────────────────────
@app.post("/download")
def download_and_upload(request: dict):
    url = request.get("url")
    filename = request.get("filename", "video")

    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url' in request body")

    local_file = download_tiktok_video(url, filename)

    try:
        response = upload_to_drive(local_file, DRIVE_FOLDER_ID)
        time.sleep(1)
        safe_delete(local_file)
        return {"status": "success", "drive_response": response}
    except Exception as e:
        safe_delete(local_file)
        raise e

# ─────────────────────────────────────────────
# Global error handler
# ─────────────────────────────────────────────
@app.exception_handler(Exception)
async def exception_handler(request: Request, exc: Exception):
    print(f"[ERROR] {exc}")
    return JSONResponse(status_code=500, content={"detail": str(exc)})
