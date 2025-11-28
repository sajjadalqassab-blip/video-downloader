import os
import re
import uuid
import requests
import yt_dlp

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Google Drive + Sheets
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Playwright
from playwright.sync_api import sync_playwright

# ============== CONFIG ==============
DRIVE_FOLDER_ID = "15slyKToMudp-SOHQx0FONS5r9HsXPE_3"

CREDS_PATH = "/etc/secrets/GOOGLE_CREDS"

SHEET_ID = "1S9qcTJ6i3OEm_6-l2fenGmqPp_AaQJrsJTZVNUGmYv0"
SHEET_RANGE = "videos!C2:G"     # C = name, E = link

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

class SheetDownloadRequest(BaseModel):
    limit: int | None = None  # optional for testing

# ============== UTILS ==============
def sanitize_filename(name: str) -> str:
    """
    Cleans filename for Windows/Linux and ensures .mp4 extension.
    """
    if not name:
        name = str(uuid.uuid4())

    name = name.strip()

    # Remove invalid filesystem chars
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)

    # Collapse spaces
    name = re.sub(r"\s+", " ", name)

    # Ensure extension
    if not name.lower().endswith(".mp4"):
        name += ".mp4"

    return name


def get_creds(scopes):
    return Credentials.from_service_account_file(
        CREDS_PATH,
        scopes=scopes
    )

# ============== GOOGLE SHEETS READ ==============
def read_sheet_rows() -> list[dict]:
    creds = get_creds([
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    sheets_service = build("sheets", "v4", credentials=creds)

    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=SHEET_RANGE
    ).execute()

    values = resp.get("values", [])
    rows = []

    for i, row in enumerate(values, start=2):
        name = row[0].strip() if len(row) > 0 and row[0] else ""

        cell_links = row[2].strip() if len(row) > 2 and row[2] else ""  # E
        existing_links = row[3].strip() if len(row) > 3 and row[3] else ""  # F
        status = row[4].strip().upper() if len(row) > 4 and row[4] else ""  # G

        # Skip rows already done or partially done
        if status in ("DONE", "PARTIAL"):
            continue

        if not cell_links:
            continue

        urls = [u.strip() for u in cell_links.splitlines() if u.strip()]

        rows.append({
            "row_index": i,
            "name": name,
            "urls": urls,
            "existing_drive_links": existing_links,
            "existing_status": status,
        })

    return rows

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

        for _ in range(12):
            page.evaluate("window.scrollBy(0, 1000)")
            page.wait_for_timeout(400)

        video_sources = []

        try:
            sources = page.query_selector_all("video source")
            for s in sources:
                src = s.get_attribute("src")
                if src and src.startswith("http"):
                    video_sources.append(src)
        except:
            pass

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

    tmp_outfile = f"{uuid.uuid4()}.mp4"

    secret_cookie_path = "/etc/secrets/INSTAGRAM_COOKIES"
    tmp_cookie_path = None

    # copy secret cookies to /tmp because /etc/secrets is read-only
    if os.path.exists(secret_cookie_path):
        tmp_cookie_path = "/tmp/ig_cookies.txt"
        with open(secret_cookie_path, "r") as src, open(tmp_cookie_path, "w") as dst:
            dst.write(src.read())
        print("[INFO] Copied INSTAGRAM_COOKIES to /tmp")

    ydl_opts = {
        "outtmpl": tmp_outfile,
        "format": "mp4",
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
    }

    # env var cookies override
    ig_cookies = os.getenv("IG_COOKIES")
    if "instagram.com" in url and ig_cookies:
        tmp_cookie_path = "/tmp/ig_cookies_env.txt"
        with open(tmp_cookie_path, "w") as f:
            f.write(ig_cookies)
        print("[INFO] Instagram cookies loaded from env var")

    if tmp_cookie_path:
        ydl_opts["cookiefile"] = tmp_cookie_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print("[ERROR] yt-dlp failed:", str(e))
        return None

    print(f"[OK] yt-dlp download complete → {tmp_outfile}")
    return tmp_outfile

# ============== GOOGLE DRIVE UPLOAD ==============
def upload_to_drive(local_path, filename):
    creds = get_creds(["https://www.googleapis.com/auth/drive"])
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

# ============== SHEET WRITEBACK ==============
def write_result_to_sheet(row_index: int, drive_links: str = "", status: str = "DONE"):
    creds = get_creds([
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    sheets_service = build("sheets", "v4", credentials=creds)

    range_to_write = f"videos!F{row_index}:G{row_index}"

    sheets_service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=range_to_write,
        valueInputOption="RAW",
        body={"values": [[drive_links, status]]}
    ).execute()


# ============== CORE PROCESSOR ==============
def process_one_url(url: str, desired_name: str | None = None):
    url = url.strip()
    local_file = None

    desired_name = sanitize_filename(desired_name or str(uuid.uuid4()))

    if "aliexpress.com" in url:
        video_url = extract_aliexpress_video(url)
        if not video_url:
            raise Exception("AliExpress video not found")

        tmp_local = f"{uuid.uuid4()}.mp4"

        r = requests.get(video_url, stream=True, timeout=60)
        with open(tmp_local, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                f.write(chunk)

        local_file = tmp_local

    else:
        local_file = download_with_ytdlp(url)
        if not local_file:
            raise Exception("yt-dlp failed")

    uploaded = upload_to_drive(local_file, desired_name)

    try:
        os.remove(local_file)
        print(f"[OK] Deleted local file")
    except:
        print("[WARN] Could not delete file")

    return uploaded, desired_name

# ============== MAIN ENDPOINT (single url) ==============
@app.post("/download")
def download_video(request: DownloadRequest):
    try:
        uploaded, final_name = process_one_url(request.url, request.filename)
        return {"success": True, "filename": final_name, "drive_file": uploaded}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============== SHEET ENDPOINT ==============
@app.post("/download-from-sheet")
def download_from_sheet(request: SheetDownloadRequest):
    rows = read_sheet_rows()

    if request.limit:
        rows = rows[: request.limit]

    results = []

    for r in rows:
        row_index = r["row_index"]
        base_name = r["name"]
        urls = r["urls"]

        row_success = True
        drive_links_collected = []
        row_results = []

        for idx, url in enumerate(urls, start=1):
            # make unique filename per link in same cell
            per_link_name = base_name if idx == 1 else f"{base_name} ({idx})"

            try:
                uploaded, final_name = process_one_url(url, per_link_name)
                drive_url = uploaded.get("webViewLink") or ""

                drive_links_collected.append(drive_url)

                row_results.append({
                    "url": url,
                    "filename": final_name,
                    "success": True,
                    "drive_file": uploaded
                })

            except Exception as e:
                row_success = False
                row_results.append({
                    "url": url,
                    "filename": sanitize_filename(per_link_name),
                    "success": False,
                    "error": str(e)
                })

        # ✅ after ALL links:
        if row_success:
            write_result_to_sheet(
                row_index,
                "\n".join(drive_links_collected),
                "DONE"
            )
            final_status = "DONE"
        else:
            # some failed
            write_result_to_sheet(
                row_index,
                "\n".join(drive_links_collected),
                "PARTIAL"
            )
            final_status = "PARTIAL"

        results.append({
            "row_index": row_index,
            "name": base_name,
            "status": final_status,
            "links_count": len(urls),
            "drive_links": drive_links_collected,
            "items": row_results
        })

    return {
        "success": True,
        "count": len(results),
        "results": results
    }