import os
import re
import sqlite3
import subprocess
import shutil
import json
import math
import logging
import urllib.request
import tarfile
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shortsflow")

app = FastAPI(title="ShortsFlow Social Media AI Backend")

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://dangerous-zonda-anushkamalshan6-62ecb98b.koyeb.app"
).strip().rstrip("/")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

DB_PATH = os.environ.get(
    "SHORTSFLOW_DB_PATH", os.path.join(PROJECT_DIR, "shortsflow.db")
).strip()
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.abspath(os.path.join(PROJECT_DIR, DB_PATH))

# ස්වයංක්‍රීයව FFmpeg බිනරි එක සර්වර් එකට ලබා ගැනීම (Auto Setup FFmpeg)
LOCAL_FFMPEG_DIR = os.path.join(PROJECT_DIR, "ffmpeg_bin")
FFMPEG_BIN = os.path.join(LOCAL_FFMPEG_DIR, "ffmpeg")

def ensure_ffmpeg():
    global FFMPEG_BIN
    if os.path.isfile(FFMPEG_BIN) and os.access(FFMPEG_BIN, os.X_OK):
        return FFMPEG_BIN
    
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        FFMPEG_BIN = sys_ffmpeg
        return FFMPEG_BIN

    os.makedirs(LOCAL_FFMPEG_DIR, exist_ok=True)
    tar_path = os.path.join(LOCAL_FFMPEG_DIR, "ffmpeg.tar.xz")
    url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
    
    try:
        logger.info("Downloading static FFmpeg binary...")
        urllib.request.urlretrieve(url, tar_path)
        with tarfile.open(tar_path, "r:xz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("ffmpeg") or member.name.endswith("ffprobe"):
                    member.name = os.path.basename(member.name)
                    tar.extract(member, LOCAL_FFMPEG_DIR)
        os.remove(tar_path)
        for f in ["ffmpeg", "ffprobe"]:
            fp = os.path.join(LOCAL_FFMPEG_DIR, f)
            if os.path.exists(fp):
                os.chmod(fp, 0o755)
        if os.path.isfile(FFMPEG_BIN):
            logger.info("FFmpeg successfully set up locally!")
            return FFMPEG_BIN
    except Exception as e:
        logger.error(f"Failed to auto-download ffmpeg: {e}")
    
    return "ffmpeg"

ensure_ffmpeg()

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "anushkamalshan666@gmail.com").strip().casefold()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "@Anushka*malshan2005")

def normalize_email(email: Any) -> str:
    return str(email or "").strip().casefold()

def safe_remove(path: str | None) -> None:
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def read_json_body(body: Any) -> dict:
    if not isinstance(body, dict):
        return {}
    return body

def get_first_string(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None:
            value = str(value).strip()
            if value:
                return value
    return ""

def clean_viral_title(title: str) -> str:
    if not title:
        return "Viral Highlight Video"
    cleaned = re.sub(r'[\d,.]+[KM]?\s*(views?|likes?|shares?|reactions?|comments?).*?[\|–\-—]', '', title, flags=re.IGNORECASE)
    cleaned = re.sub(r'[\d,.]+[KM]?\s*(views?|likes?|shares?|reactions?|comments?)', '', cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" |–—•·")
    if not cleaned:
        cleaned = title[:40]
    return cleaned.strip()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or PROJECT_DIR, exist_ok=True)
    conn = get_db_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                password TEXT,
                is_vip INTEGER DEFAULT 0,
                vip_expiry TEXT,
                monthly_clips_used INTEGER DEFAULT 0,
                last_reset_month TEXT,
                device_id TEXT
            )
        """)
        if ADMIN_EMAIL:
            cursor = conn.cursor()
            cursor.execute("SELECT email FROM users WHERE LOWER(TRIM(email)) = ?", (normalize_email(ADMIN_EMAIL),))
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO users VALUES (?, ?, 1, '2099-12-31', 0, ?, 'ADMIN_DEVICE')",
                    (normalize_email(ADMIN_EMAIL), ADMIN_PASSWORD, datetime.now().strftime("%Y-%m"))
                )
            else:
                cursor.execute(
                    "UPDATE users SET is_vip = 1, vip_expiry = '2099-12-31' WHERE LOWER(TRIM(email)) = ?",
                    (normalize_email(ADMIN_EMAIL),)
                )
        conn.commit()
    finally:
        conn.close()

init_db()

@app.get("/")
def root():
    return {"service": "ShortsFlow Social Media AI Backend", "status": "online"}

@app.get("/health")
def health():
    return {"status": "ok", "ffmpeg_available": os.path.isfile(FFMPEG_BIN)}

app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")

@app.post("/auth/register")
async def register(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    data = read_json_body(body)
    email = normalize_email(get_first_string(data, "email", "user_email"))
    password = get_first_string(data, "password")
    device_id = get_first_string(data, "device_id") or "UNKNOWN"

    if not email or not password:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Email සහ Password අවශ්‍යයි."})
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        current_month = datetime.now().strftime("%Y-%m")
        cursor.execute(
            "INSERT INTO users (email, password, is_vip, vip_expiry, monthly_clips_used, last_reset_month, device_id) VALUES (?, ?, 0, NULL, 0, ?, ?)",
            (email, password, current_month, device_id),
        )
        conn.commit()
        return {"status": "success", "message": "ගිණුම සාදන ලදී!"}
    except sqlite3.IntegrityError:
        return JSONResponse(status_code=400, content={"status": "error", "message": "මෙම Email එක දැනටමත් ලියාපදිංචි කර ඇත."})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(exc)})
    finally:
        conn.close()

@app.post("/auth/login")
async def login(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    data = read_json_body(body)
    email = normalize_email(get_first_string(data, "email", "user_email"))
    password = get_first_string(data, "password")
    
    if not email or not password:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Email සහ Password අවශ්‍යයි."})

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT email, password, is_vip, vip_expiry, monthly_clips_used, last_reset_month FROM users WHERE LOWER(TRIM(email)) = ?", (email,))
        user = cursor.fetchone()
    finally:
        conn.close()

    if not user or user[1] != password:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Email හෝ Password වැරදියි."})
    
    user_email, _, is_vip, vip_expiry, clips_used, last_reset = user
    current_month = datetime.now().strftime("%Y-%m")

    if last_reset != current_month:
        conn = get_db_connection()
        try:
            conn.execute("UPDATE users SET monthly_clips_used = 0, last_reset_month = ? WHERE LOWER(TRIM(email)) = ?", (current_month, user_email))
            conn.commit()
            clips_used = 0
        finally:
            conn.close()

    is_admin = bool(ADMIN_EMAIL) and normalize_email(user_email) == normalize_email(ADMIN_EMAIL)
    if is_admin:
        is_vip = 1
        vip_expiry = "2099-12-31"

    return {
        "status": "success",
        "email": user_email,
        "is_vip": bool(is_vip),
        "is_admin": is_admin,
        "links_used": clips_used or 0,
    }

@app.post("/auth/forgot-password")
async def forgot_password(request: Request):
    return {"status": "success", "message": "OTP යවන ලදී", "demo_otp": "123456"}

@app.post("/auth/reset-password")
async def reset_password(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    data = read_json_body(body)
    email = normalize_email(get_first_string(data, "email"))
    new_password = get_first_string(data, "new_password", "password")
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET password = ? WHERE LOWER(TRIM(email)) = ?", (new_password, email))
        conn.commit()
    finally:
        conn.close()
    return {"status": "success", "message": "Password යාවත්කාලීන විය!"}

@app.post("/admin/activate-vip")
async def activate_vip(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    data = read_json_body(body)
    target_user_email = normalize_email(get_first_string(data, "target_user_email", "user_email"))
    expiry = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET is_vip = 1, vip_expiry = ? WHERE LOWER(TRIM(email)) = ?", (expiry, target_user_email))
        conn.commit()
    finally:
        conn.close()
    return {"status": "success", "message": "VIP සක්‍රීය විය!"}

def download_social_video(url: str, output_path: str):
    clean_url = (url or "").strip()
    ydl_opts = {
        "format": "best[height<=480]/best",
        "outtmpl": output_path,
        "overwrites": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36",
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(clean_url, download=True)
        downloaded_path = ydl.prepare_filename(info)
        candidates = [output_path, os.path.splitext(output_path)[0] + ".mp4", downloaded_path]
        actual_path = next((p for p in candidates if os.path.isfile(p)), output_path)
        if actual_path != output_path and os.path.isfile(actual_path):
            shutil.move(actual_path, output_path)
        
        duration = float(info.get("duration") or 0)
        if duration <= 1:
            try:
                cmd = [FFMPEG_BIN.replace("ffmpeg", "ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", output_path]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                duration = float(res.stdout.strip())
            except Exception:
                duration = 30.0

        raw_title = info.get("title") or "Viral Video"
        return clean_viral_title(raw_title), max(3.0, duration)

@app.post("/process-video")
async def process_video(request: Request):
    try:
        body = read_json_body(await request.json())
    except Exception:
        body = {}

    user_email = normalize_email(get_first_string(body, "email", "user_email"))
    url_to_process = get_first_string(body, "video_url", "youtube_url", "url", "link")

    if not user_email or not url_to_process:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Email සහ Video URL අවශ්‍යයි."})

    conn = get_db_connection()
    raw_video = os.path.join(PROJECT_DIR, f"temp_{int(datetime.now().timestamp() * 1000)}.mp4")
    output_data = []

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT is_vip, monthly_clips_used, last_reset_month FROM users WHERE LOWER(TRIM(email)) = ?", (user_email,))
        user = cursor.fetchone()
        
        if not user:
            return JSONResponse(status_code=404, content={"status": "error", "message": "පරිශීලකයා හමු නොවීය."})

        is_vip, clips_used, last_reset = user
        current_month = datetime.now().strftime("%Y-%m")

        if last_reset != current_month:
            clips_used = 0
            cursor.execute("UPDATE users SET monthly_clips_used = 0, last_reset_month = ? WHERE LOWER(TRIM(email)) = ?", (current_month, user_email))
            conn.commit()

        is_admin = user_email == ADMIN_EMAIL
        
        if not is_vip and not is_admin and clips_used >= 15:
            return JSONResponse(status_code=403, content={"status": "error", "message": "මෙම මස සඳහා ඔබට හිමි Free වීඩියෝ 15ක සීමාව අවසන් වී ඇත. VIP ලබාගන්න."})

        video_title, total_duration = download_social_video(url_to_process, raw_video)

        if not is_vip and not is_admin and total_duration > 600:
            return JSONResponse(status_code=403, content={"status": "error", "message": "Free Plan එකේ උපරිම විනාඩි 10 ක වීඩියෝ පමණි."})

        part_duration = 180
        total_parts = max(1, math.ceil(total_duration / part_duration))
        if not is_vip and not is_admin and total_parts > 4:
            total_parts = 4

        for i in range(total_parts):
            start_sec = i * part_duration
            if start_sec >= total_duration:
                break
            current_clip_len = min(part_duration, total_duration - start_sec)
            if current_clip_len <= 2:
                break

            clip_name = f"repurpose_{int(datetime.now().timestamp())}_{i + 1}.mp4"
            final_path = os.path.join(OUTPUT_DIR, clip_name)

            vf_filter = "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2"
            if not is_vip and not is_admin:
                vf_filter += ",drawtext=text='ShortsFlow AI':x=w-tw-25:y=h-th-40:fontsize=22:fontcolor=white@0.55"

            command = [
                FFMPEG_BIN, "-y", "-ss", str(start_sec), "-i", raw_video, "-t", str(int(current_clip_len)),
                "-vf", vf_filter, "-c:v", "libx264", "-crf", "28",
                "-preset", "ultrafast", "-threads", "2",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", final_path,
            ]

            subprocess.run(command, check=True, timeout=300, capture_output=True, text=True)

            output_data.append({
                "clip_url": f"{PUBLIC_BASE_URL}/output/{clip_name}",
                "title": f"{video_title} (Part {i+1})" if total_parts > 1 else video_title,
                "description": f"Viral highlight part {i+1} from {video_title}",
                "hashtags": "#Shorts #Viral #Trending #FYP",
            })

        if not is_vip and not is_admin:
            cursor.execute("UPDATE users SET monthly_clips_used = monthly_clips_used + 1 WHERE LOWER(TRIM(email)) = ?", (user_email,))
            conn.commit()

        return {"status": "success", "results": output_data}

    except Exception as exc:
        logger.exception("Processing error")
        return JSONResponse(status_code=500, content={"status": "error", "message": f"දෝෂයක් ඇති විය: {str(exc)[:180]}"})
    finally:
        safe_remove(raw_video)
        conn.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
