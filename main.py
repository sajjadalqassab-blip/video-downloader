import os
import time
import json
import yt_dlp
import requests
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

# ─────────────────────────────────────────────
# Parse credentials from JSON env var
# ─────────────────────────────────────────────
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
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    if creds_path and os.path.exists(creds_path):
        creds = service_account.Credentials.from_service_account_file(
            creds_path, scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    raise HTTPException(status_code=500, detail="No valid Google Drive credentials available")

# ─────────────────────────────────────────────
# Upload to Google Drive
# ─────────────────────────────────────────────
def upload_to_drive(file_path: str, folder_id: str):
    service = get_drive_service()

    if not os.path.exists(file_path):
        raise HTTPException(status_code=400, detail=f"File not found: {file_path}")

    # Check folder access
    try:
        folder_meta = service.files().get(
            fileId=folder_id,
            fields="id,name,mimeType,driveId,trashed",
            supportsAllDrives=True,
        ).execute()
        print(f"[OK] Uploading into folder: {folder_meta['name']} (driveId={folder_meta.get('driveId')})")
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
            while response is None:
                _, response = request.next_chunk()
            print(f"[OK] File uploaded → {response['id']}")

            # Make public
            if MAKE_FILE_PUBLIC:
                service.permissions().create(
                    fileId=response["id"],
                    body={"role": "reader", "type": "anyone"},
                    supportsAllDrives=True,
                ).execute()
                print("[OK] File made public.")

            return response
        except Exception as e:
            print(f"[WARN] Upload failed (attempt {attempt}/5): {e}")
            time.sleep(attempt * 5)

    raise HTTPException(status_code=502, detail="Upload failed after 5 attempts")

# ─────────────────────────────────────────────
# Universal video downloader (TikTok/Instagram/AliExpress)
# ─────────────────────────────────────────────
def download_tiktok_video(url: str, filename: str):
    save_path = os.path.join(os.getcwd(), f"{filename}.mp4")
    print(f"[INFO] Downloading {url}")

    # --- AliExpress Fix: Force CN region & stop redirect to aliexpress.us ---
    if "aliexpress." in url:
        print("[INFO] Applying Anti-Redirect Patch for AliExpress...")
        
        # Force CN region (prevents redirect)
        cookies = {
            "aep_usuc_f": "site=glo&c_tp=USD&region=CN&b_locale=en_US"
        }

        # Force Accept-Language = CN/EN
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.aliexpress.com/",
        }

        # Get fixed .com URL (prevents .us redirect)
        try:
            r = requests.get(url, headers=headers, cookies=cookies, timeout=15, allow_redirects=False)
            if r.status_code in (301, 302) and "aliexpress.us" in r.headers.get("Location", ""):
                print("[WARN] AliExpress redirect detected → forcing .com")
                url = url.replace("aliexpress.us", "aliexpress.com")
        except Exception as e:
            print(f"[WARN] Anti-redirect check failed: {e}")

    else:
        # Normal behavior for TikTok/Instagram
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

        cookies = {}

    # ---------------------------------------------------------------------

    ydl_opts = {
        "outtmpl": save_path,
        "format": "bestvideo+bestaudio/best",
        "quiet": False,
        "merge_output_format": "mp4",
        "ignoreerrors": True,
        "http_headers": headers,
        "cookies": cookies,
        "source_address": "0.0.0.0",
    }

    # Enable official AliExpress extractor (now works because redirect blocked)
    if "aliexpress" in url:
        ydl_opts["extractor_args"] = {
            "aliexpress": {"language": ["en_US"], "currency": ["USD"]}
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if not os.path.exists(save_path):
            raise HTTPException(status_code=500, detail="Download failed: file not created")

        print(f"[OK] Downloaded → {save_path}")
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
        raise HTTPException(status_code=400, detail="Missing URL")

    local_file = download_tiktok_video(url, filename)

    try:
        response = upload_to_drive(local_file, DRIVE_FOLDER_ID)
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
