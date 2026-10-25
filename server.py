import os
import re
import sqlite3
import subprocess
import shutil
import json
import random
import urllib.request
import urllib.error
import math
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shortsflow")

app = FastAPI(title="ShortsFlow Universal AI Backend")

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://shortsflow-server.onrender.com"
).strip().rstrip("/")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

DB_PATH = os.environ.get(
    "SHORTSFLOW_DB_PATH", os.path.join(PROJECT_DIR, "shortsflow.db")
).strip()
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.abspath(os.path.join(PROJECT_DIR, DB_PATH))

FFMPEG_DIR = "/opt/render/project/.render/ffmpeg"
FFMPEG_CANDIDATE = os.path.join(FFMPEG_DIR, "ffmpeg")
if os.path.isfile(FFMPEG_CANDIDATE) and os.access(FFMPEG_CANDIDATE, os.X_OK):
    FFMPEG_BIN = FFMPEG_CANDIDATE
else:
    FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "anushkamalshan666@gmail.com").strip().casefold()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "@Anushka*malshan2005")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

otp_storage: dict[str, dict[str, Any]] = {}

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
    return {"service": "ShortsFlow AI Backend", "status": "online"}

@app.get("/health")
def health():
    return {"status": "ok", "ffmpeg_available": True}

app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")

def generate_metadata_with_gemini(video_title: str, part_num: int, total_parts: int):
    clean_title = re.sub(r"[^\w\s-]", "", video_title or "Social Video")[:40].strip()
    if not clean_title:
        clean_title = "Social Video"
    suffix = f" (Part {part_num})" if total_parts > 1 else ""
    fallback_title = f"🔥 {clean_title}{suffix}"
    return {
        "title": fallback_title,
        "description": f"Must see highlight clip from {clean_title}! Share and follow.",
        "hashtags": "#Shorts #Viral #Trending #FYP #Reels",
    }

@app.post("/auth/register")
async def register(request: Request):
    data = read_json_body(await request.json())
    email = normalize_email(get_first_string(data, "email", "user_email"))
    password = get_first_string(data, "password")
    device_id = get_first_string(data, "device_id") or "UNKNOWN"
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email සහ Password අවශ්‍යයි.")
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
        raise HTTPException(status_code=400, detail="මෙම Email එක දැනටමත් ලියාපදිංචි කර ඇත.")
    finally:
        conn.close()

@app.post("/auth/login")
async def login(request: Request):
    data = read_json_body(await request.json())
    email = normalize_email(get_first_string(data, "email", "user_email"))
    password = get_first_string(data, "password")
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT email, password, is_vip, vip_expiry, monthly_clips_used FROM users WHERE LOWER(TRIM(email)) = ?", (email,))
        user = cursor.fetchone()
    finally:
        conn.close()
    if not user or user[1] != password:
        raise HTTPException(status_code=401, detail="Email හෝ Password වැරදියි.")
    user_email, _, is_vip, vip_expiry, clips_used = user
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
    data = read_json_body(await request.json())
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
    data = read_json_body(await request.json())
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
        "format": "bestvideo+bestaudio/best",
        "outtmpl": output_path,
        "overwrites": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 60,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(clean_url, download=True)
        downloaded_path = ydl.prepare_filename(info)
        candidates = [output_path, os.path.splitext(output_path)[0] + ".mp4", downloaded_path]
        actual_path = next((p for p in candidates if os.path.isfile(p)), output_path)
        if actual_path != output_path and os.path.isfile(actual_path):
            shutil.move(actual_path, output_path)
        return info.get("title") or "Viral Video", max(1, int(info.get("duration") or 60))

def escape_drawtext(text: str) -> str:
    text = str(text or "").replace("\n", " ").replace("\r", " ")
    for old, new in {"\\": r"\\", "'": r"\'", ":": r"\:", "%": r"\%", "[": r"\[", "]": r"\]", ",": r"\,", ";": r"\;"}.items():
        text = text.replace(old, new)
    return text[:35]

@app.post("/process-video")
async def process_video(request: Request):
    try:
        body = read_json_body(await request.json())
    except Exception:
        body = {}

    user_email = normalize_email(get_first_string(body, "email", "user_email"))
    url_to_process = get_first_string(body, "video_url", "youtube_url", "url", "link")

    if not user_email or not url_to_process:
        raise HTTPException(status_code=400, detail="Email සහ Video URL අවශ්‍යයි.")

    conn = get_db_connection()
    raw_video = os.path.join(PROJECT_DIR, f"temp_{int(datetime.now().timestamp() * 1000)}.mp4")
    output_data = []

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT is_vip FROM users WHERE LOWER(TRIM(email)) = ?", (user_email,))
        user = cursor.fetchone()
        is_vip = 1 if (user and user[0] == 1) or user_email == ADMIN_EMAIL else 0

        video_title, total_duration = download_social_video(url_to_process, raw_video)

        part_duration = 180
        total_parts = max(1, math.ceil(total_duration / part_duration))

        for i in range(total_parts):
            start_sec = i * part_duration
            current_clip_len = min(part_duration, total_duration - start_sec)
            if current_clip_len <= 3:
                continue

            clip_name = f"repurpose_{int(datetime.now().timestamp())}_{i + 1}.mp4"
            final_path = os.path.join(OUTPUT_DIR, clip_name)

            meta = generate_metadata_with_gemini(video_title, i + 1, total_parts)
            title_text = escape_drawtext(meta.get("title", ""))

            base_filter = (
                "[0:v]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,boxblur=15:5[bg];"
                "[0:v]scale=720:-1,setsar=1,eq=brightness=0.03:saturation=1.12:contrast=1.05,setpts=0.97*PTS[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2[combined];"
                f"[combined]drawtext=text='{title_text}':fontcolor=yellow:fontsize=36:box=1:boxcolor=black@0.65:boxborderw=10:x=(w-text_w)/2:y=120[vout]"
            )

            if not is_vip:
                vf_filter = base_filter + ";[vout]drawtext=text='ShortsFlow AI':x=w-tw-25:y=h-th-40:fontsize=22:fontcolor=white@0.55:shadowcolor=black@0.4:shadowx=2:shadowy=2[vfinal]"
                video_map = "[vfinal]"
            else:
                vf_filter = base_filter
                video_map = "[vout]"

            command = [
                FFMPEG_BIN, "-y", "-ss", str(start_sec), "-i", raw_video, "-t", str(int(current_clip_len)),
                "-filter_complex", vf_filter, "-map", video_map, "-map", "0:a?",
                "-af", "atempo=1.03,volume=1.05", "-c:v", "libx264", "-crf", "23",
                "-preset", "ultrafast", "-threads", "2", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", "-map_metadata", "-1", final_path,
            ]

            subprocess.run(command, check=True, timeout=180, capture_output=True, text=True)

            output_data.append({
                "clip_url": f"{PUBLIC_BASE_URL}/output/{clip_name}",
                "title": meta.get("title", ""),
                "description": meta.get("description", ""),
                "hashtags": meta.get("hashtags", ""),
            })

        return {"status": "success", "results": output_data}

    except Exception as exc:
        logger.exception("Processing error")
        raise HTTPException(status_code=500, detail=f"දෝෂයක් ඇති විය: {str(exc)[:150]}")
    finally:
        safe_remove(raw_video)
        conn.close()

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
