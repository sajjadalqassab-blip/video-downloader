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

    # Optional check that folder is accessible
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
# AliExpress product video extractor (Option A)
# ─────────────────────────────────────────────
def extract_aliexpress_video(url: str) -> str:
    """
    Extracts the main PRODUCT video URL from an AliExpress product page
    (works for .com and .us after redirect).
    """
    print(f"[INFO] Trying AliExpress HTML extractor → {url}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    # Cookie to force global site (sometimes helps avoid weird variants)
    cookies = {
        "aep_usuc_f": "site=glo&region=SA&b_locale=en_US"
    }

    try:
        r = requests.get(url, headers=headers, cookies=cookies, timeout=20)
        html = r.text

        # 1) Look for "videoInfos" JSON (main product videos)
        m = re.search(r'"videoInfos"\s*:\s*(\[[^\]]+\])', html)
        if m:
            block = m.group(1)
            # unescape common sequences
            block = block.replace('\\u002F', '/')
            block = block.replace('\\\\/', '/')

            try:
                video_infos = json.loads(block)
                if isinstance(video_infos, list) and video_infos:
                    vi = video_infos[0]
                    # Common keys: videoUrl, src, url
                    url_key = vi.get("videoUrl") or vi.get("src") or vi.get("url")
                    if url_key:
                        print(f"[OK] AliExpress videoInfos → {url_key}")
                        return url_key
            except Exception as e:
                print(f"[WARN] Failed to parse videoInfos JSON: {e}")

        # 2) Fallback: search for videoUrl anywhere in HTML
        m2 = re.search(r'"videoUrl"\s*:\s*"(.*?)"', html)
        if m2:
            vid = m2.group(1).replace('\\u002F', '/').replace('\\\\/', '/')
            print(f"[OK] AliExpress fallback videoUrl → {vid}")
            return vid

        # 3) Fallback: cloud.video.taobao.com CDN links
        m3 = re.findall(r'https://cloud\.video\.taobao\.com[^\"]+', html)
        if m3:
            print(f"[OK] AliExpress taobao CDN → {m3[0]}")
            return m3[0]

        print("[WARN] No AliExpress product video found in HTML.")
        return None

    except Exception as e:
        print(f"[ERROR] AliExpress extractor error: {e}")
        return None


# ─────────────────────────────────────────────
# Universal video downloader
# ─────────────────────────────────────────────
def download_tiktok_video(url: str, filename: str):
    save_path = os.path.join(os.getcwd(), f"{filename}.mp4")
    print(f"[INFO] Downloading {url}")

    # ── AliExpress branch: HTML parse + direct MP4 download ──
    if "aliexpress.com" in url or "aliexpress.us" in url:
        video_direct = extract_aliexpress_video(url)
        if not video_direct:
            raise HTTPException(
                status_code=500,
                detail="AliExpress: No downloadable product video found"
            )

        print(f"[INFO] AliExpress direct video URL → {video_direct}")

        try:
            with requests.get(video_direct, stream=True, timeout=60) as r:
                if r.status_code != 200:
                    raise HTTPException(
                        status_code=500,
                        detail=f"AliExpress video download failed (status {r.status_code})"
                    )
                with open(save_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
            print(f"[OK] AliExpress video saved → {save_path}")
            return save_path
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"AliExpress direct download error: {e}")

    # ── Normal branch: TikTok / Instagram / others via yt_dlp ──
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": url,
    }

    ydl_opts = {
        "outtmpl": save_path,
        "format": "bestvideo+bestaudio/best",
        "quiet": False,
        "merge_output_format": "mp4",
        "ignoreerrors": True,
        "http_headers": headers,
        "source_address": "0.0.0.0",
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
