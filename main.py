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
def extract_aliexpress_video(url: str) -> str:
    print(f"[INFO] Trying AliExpress extractor → {url}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        )
    }

    try:
        html = requests.get(url, headers=headers, timeout=20).text

        # 1) Extract video data from runParams JSON blob
        json_blob = re.search(r'window\.runParams\s*=\s*(\{.*?\});', html)
        if json_blob:
            try:
                data = json.loads(json_blob.group(1))

                # Path: data → props → video → url
                video_url = (
                    data.get("data", {})
                        .get("props", {})
                        .get("video", {})
                        .get("url")
                )

                if video_url:
                    print(f"[OK] AliExpress JSON video → {video_url}")
                    return video_url

                # Some versions store it under "videos" list
                videos = (
                    data.get("data", {})
                        .get("props", {})
                        .get("videos", [])
                )

                if isinstance(videos, list) and len(videos) > 0:
                    if "src" in videos[0]:
                        print(f"[OK] AliExpress JSON list video → {videos[0]['src']}")
                        return videos[0]["src"]

            except Exception as e:
                print(f"[WARN] JSON parse failed: {e}")

        # 2) Look for cloudvideo CDN URLs
        matches = re.findall(r'https:\\/\\/cloudvideo[a-zA-Z0-9\\.\\/\\-_]+', html)
        if matches:
            clean = matches[0].replace("\\/", "/")
            print(f"[OK] AliExpress cloudvideo → {clean}")
            return clean

        # 3) Old "videoUrl" format
        matches = re.findall(r'"videoUrl":"(.*?)"', html)
        if matches:
            video_url = matches[0].replace("\\u002F", "/")
            print(f"[OK] AliExpress fallback video → {video_url}")
            return video_url

        print("[WARN] No AliExpress video found.")
        return None

    except Exception as e:
        print(f"[ERROR] AliExpress extractor error → {e}")
        return None

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
