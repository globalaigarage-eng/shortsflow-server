import os
import re
import json
import time
import uuid
import shutil
import sqlite3
import logging
import asyncio
import secrets
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests
import yt_dlp
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# Logging Configuration
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("shortsflow")

APP_NAME = "ShortsFlow AI Backend"
PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("SHORTSFLOW_DATA_DIR", str(PROJECT_DIR / "data"))).resolve()
OUTPUT_DIR = Path(os.getenv("SHORTSFLOW_OUTPUT_DIR", str(DATA_DIR / "output"))).resolve()
TEMP_DIR = Path(os.getenv("SHORTSFLOW_TEMP_DIR", str(DATA_DIR / "tmp"))).resolve()
DB_PATH = Path(os.getenv("SHORTSFLOW_DB_PATH", str(DATA_DIR / "shortsflow.db"))).resolve()

for directory in (DATA_DIR, OUTPUT_DIR, TEMP_DIR, DB_PATH.parent):
    directory.mkdir(parents=True, exist_ok=True)

# Render Custom FFmpeg Path detection
FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"
render_ffmpeg = Path.home() / ".render" / "ffmpeg" / "ffmpeg"
if render_ffmpeg.is_file() and os.access(render_ffmpeg, os.X_OK):
    FFMPEG_BIN = str(render_ffmpeg)
    FFPROBE_BIN = str(Path.home() / ".render" / "ffmpeg" / "ffprobe")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "anushkamalshan666@gmail.com").strip().casefold()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "@Anushka*malshan2005")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()
FREE_MONTHLY_JOBS = int(os.getenv("FREE_MONTHLY_JOBS", "15"))
PART_DURATION_SECONDS = int(os.getenv("PART_DURATION_SECONDS", "180"))  # විනාඩි 3 කෑලි වලට
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1")))
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "1500"))
JOB_RETENTION_DAYS = int(os.getenv("JOB_RETENTION_DAYS", "3"))
PORT = int(os.getenv("PORT", "8000"))

SUPPORTED_HOSTS = (
    "tiktok.com", "facebook.com", "fb.watch", "instagram.com",
    "x.com", "twitter.com", "pinterest.com", "pin.it", "youtube.com", "youtu.be"
)

app = FastAPI(title=APP_NAME, version="2.1.0")
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")

worker_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
worker_task = None

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def current_month() -> str:
    return datetime.now().strftime("%Y-%m")

def normalize_email(value: Any) -> str:
    return str(value or "").strip().casefold()

def hash_password(password: str) -> str:
    import hashlib
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240_000)
    return "pbkdf2_sha256$240000$" + salt.hex() + "$" + digest.hex()

def verify_password(password: str, encoded: str) -> bool:
    import hashlib
    try:
        scheme, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return secrets.compare_digest(password, encoded)
        calculated = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return secrets.compare_digest(calculated.hex(), digest_hex)
    except Exception:
        return False

def db_connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

def init_db():
    with db_connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            password_hash TEXT,
            is_vip INTEGER NOT NULL DEFAULT 0,
            vip_expiry TEXT,
            monthly_jobs_used INTEGER NOT NULL DEFAULT 0,
            last_reset_month TEXT,
            device_id TEXT,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            source_url TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0,
            message TEXT,
            results_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """)
        
        # Admin User Insertion Fix: INSERT OR IGNORE භාවිත කර ඇත
        if ADMIN_EMAIL and ADMIN_PASSWORD:
            conn.execute("""
                INSERT OR IGNORE INTO users 
                (email, password_hash, is_vip, vip_expiry, monthly_jobs_used, last_reset_month, device_id, created_at)
                VALUES (?, ?, 1, '2099-12-31', 0, ?, 'ADMIN_DEVICE', ?)
            """, (ADMIN_EMAIL, hash_password(ADMIN_PASSWORD), current_month(), utcnow().isoformat()))
            
            # පවතින ගිණුම නම් VIP තහවුරු කිරීම
            conn.execute("UPDATE users SET is_vip=1, vip_expiry='2099-12-31' WHERE lower(trim(email))=?", (ADMIN_EMAIL,))

        # Restart වූ විට stuck jobs reset කිරීම
        conn.execute("""
            UPDATE jobs SET status='queued', message='Requeued after service restart',
            updated_at=? WHERE status='processing'
        """, (utcnow().isoformat(),))
        conn.commit()

init_db()

def get_user(email: str):
    with db_connect() as conn:
        return conn.execute("SELECT * FROM users WHERE lower(trim(email))=?", (normalize_email(email),)).fetchone()

def update_job(job_id: str, **fields):
    if not fields:
        return
    fields["updated_at"] = utcnow().isoformat()
    assignments = ", ".join(f"{k}=?" for k in fields.keys())
    with db_connect() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE job_id=?", [*fields.values(), job_id])
        conn.commit()

def public_file_url(filename: str) -> str:
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/output/{filename}"
    return f"/output/{filename}"

def clean_title(value: str, limit: int = 90) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    value = re.sub(r"[\x00-\x1f]", "", value)
    return value[:limit] or "Video Highlight"

def generate_metadata(video_title: str, part_num: int, total_parts: int) -> dict:
    title = clean_title(video_title, 70)
    suffix = f" | Part {part_num}" if total_parts > 1 else ""
    fallback = {
        "title": f"🔥 {title}{suffix}"[:80],
        "description": f"Highlight part {part_num} of {title}! Like and subscribe for more.",
        "hashtags": "#Shorts #Viral #Trending #FYP #Reels"
    }
    if not GEMINI_API_KEY:
        return fallback

    prompt = f"Video: '{title}'. Part {part_num}/{total_parts}. Return ONLY strict JSON with keys: 'title', 'description', 'hashtags'."
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    try:
        res = requests.post(
            url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=8
        )
        if res.status_code == 200:
            data = res.json()
            raw_text = data['candidates'][0]['content']['parts'][0]['text']
            cleaned = re.sub(r"```(json)?", "", raw_text).strip()
            parsed = json.loads(cleaned)
            return {
                "title": clean_title(parsed.get("title") or fallback["title"], 80),
                "description": clean_title(parsed.get("description") or fallback["description"], 300),
                "hashtags": clean_title(parsed.get("hashtags") or fallback["hashtags"], 150)
            }
    except Exception as exc:
        logger.warning("Gemini API error; using fallback: %s", exc)
    return fallback

def download_video(url: str, job_id: str) -> tuple[Path, str, int]:
    template = str(TEMP_DIR / f"{job_id}.%(ext)s")
    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 35,
        "retries": 3,
        "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("වීඩියෝ විස්තර ලබාගත නොහැකි විය.")
        title = clean_title(info.get("title") or "Viral Video", 100)
        duration = int(info.get("duration") or 0)

    # ගොනුව සොයාගැනීම
    found = None
    for p in TEMP_DIR.glob(f"{job_id}.*"):
        if p.is_file() and p.suffix not in (".part", ".ytdl"):
            found = p
            break
            
    if not found or not found.exists():
        raise RuntimeError("බාගත කල වීඩියෝ ගොනුව හමු නොවීය.")

    if duration <= 0:
        try:
            probe = subprocess.run(
                [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(found)],
                capture_output=True, text=True, timeout=15
            )
            val = float(probe.stdout.strip())
            if val > 0:
                duration = int(val)
        except Exception:
            duration = 180

    return found, title, duration

def split_and_transform_video(source: Path, job_id: str, title: str, duration: int) -> list[dict]:
    total_parts = max(1, (duration + PART_DURATION_SECONDS - 1) // PART_DURATION_SECONDS)
    results = []

    for index in range(total_parts):
        start = index * PART_DURATION_SECONDS
        clip_duration = min(PART_DURATION_SECONDS, duration - start)
        if clip_duration <= 2:
            continue

        output_name = f"{job_id}_part_{index + 1:03d}.mp4"
        output_path = OUTPUT_DIR / output_name

        update_job(
            job_id,
            progress=min(90, 15 + int(75 * (index + 1) / total_parts)),
            message=f"Processing Part {index + 1} of {total_parts}"
        )

        # Anti-Copyright Transformation:
        # Subtle Zoom, Color Grading, Flip, Frame rate, Speed/Pitch shift (Audio)
        vf_chain = (
            "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,"
            "eq=brightness=0.02:saturation=1.08:contrast=1.04"
        )
        af_chain = "atempo=1.03,volume=1.05"

        cmd = [
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(start), "-i", str(source), "-t", str(clip_duration),
            "-vf", vf_chain,
            "-af", af_chain,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-map_metadata", "-1",
            str(output_path)
        ]

        try:
            subprocess.run(cmd, check=True, timeout=300, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            logger.warning("Advanced FFmpeg failed, running fast copy: %s", exc.stderr)
            fallback_cmd = [
                FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", str(start), "-i", str(source), "-t", str(clip_duration),
                "-c", "copy", str(output_path)
            ]
            subprocess.run(fallback_cmd, check=True, timeout=90)

        meta = generate_metadata(title, index + 1, total_parts)
        results.append({
            "part": index + 1,
            "clip_url": public_file_url(output_name),
            "title": meta["title"],
            "description": meta["description"],
            "hashtags": meta["hashtags"]
        })

    return results

def process_job(job_id: str):
    with db_connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not job:
        return

    email, url = job["email"], job["source_url"]
    source_path = None
    try:
        update_job(job_id, status="processing", progress=10, message="Downloading video...")
        source_path, title, duration = download_video(url, job_id)
        
        update_job(job_id, progress=25, message="Transforming & Splitting (3-minute parts)...")
        results = split_and_transform_video(source_path, job_id, title, duration)
        
        update_job(
            job_id,
            status="completed",
            progress=100,
            message="Completed",
            results_json=json.dumps(results, ensure_ascii=False),
            error=None
        )
        logger.info("Job %s completed with %d clips", job_id, len(results))
    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        update_job(job_id, status="failed", progress=100, message="Failed", error=str(exc))
    finally:
        if source_path and source_path.exists():
            source_path.unlink(missing_ok=True)
        for p in TEMP_DIR.glob(f"{job_id}.*"):
            p.unlink(missing_ok=True)

async def worker_loop():
    logger.info("Background job processor started.")
    while True:
        try:
            async with worker_semaphore:
                job_id = None
                with db_connect() as conn:
                    row = conn.execute(
                        "SELECT job_id FROM jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
                    ).fetchone()
                    if row:
                        job_id = row["job_id"]
                        conn.execute(
                            "UPDATE jobs SET status='processing', message='In progress', updated_at=? WHERE job_id=?",
                            (utcnow().isoformat(), job_id)
                        )
                        conn.commit()
                
                if job_id:
                    await asyncio.to_thread(process_job, job_id)
                else:
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Worker error encountered")
            await asyncio.sleep(3)

@app.on_event("startup")
async def on_startup():
    global worker_task
    worker_task = asyncio.create_task(worker_loop())

@app.on_event("shutdown")
async def on_shutdown():
    if worker_task:
        worker_task.cancel()

@app.get("/")
def root():
    return {"service": APP_NAME, "status": "online"}

@app.get("/health")
def health():
    ffmpeg_ok = shutil.which(FFMPEG_BIN) is not None or os.path.exists(FFMPEG_BIN)
    return {
        "status": "ok" if ffmpeg_ok else "ffmpeg_missing",
        "ffmpeg_available": ffmpeg_ok,
        "public_url": PUBLIC_BASE_URL
    }

@app.post("/auth/register")
async def register(request: Request):
    data = await request.json()
    email = normalize_email(data.get("email") or data.get("user_email"))
    password = str(data.get("password") or "")
    device_id = str(data.get("device_id") or "UNKNOWN")
    if not email or "@" not in email or not password:
        raise HTTPException(status_code=400, detail="වලංගු Email සහ Password ඇතුළත් කරන්න.")

    with db_connect() as conn:
        try:
            conn.execute("""
                INSERT INTO users (email, password_hash, is_vip, vip_expiry, monthly_jobs_used, last_reset_month, device_id, created_at)
                VALUES (?, ?, 0, NULL, 0, ?, ?, ?)
            """, (email, hash_password(password), current_month(), device_id, utcnow().isoformat()))
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="මෙම Email එක දැනටමත් ලියාපදිංචි කර ඇත.")
    return {"status": "success", "message": "ගිණුම සාදන ලදී!"}

@app.post("/auth/login")
async def login(request: Request):
    data = await request.json()
    email = normalize_email(data.get("email") or data.get("user_email"))
    password = str(data.get("password") or "")
    user = get_user(email)

    if not user or not user["password_hash"] or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Email හෝ Password වැරදියි.")

    is_admin = bool(ADMIN_EMAIL) and email == ADMIN_EMAIL
    vip = bool(user["is_vip"]) or is_admin
    return {
        "status": "success",
        "email": email,
        "is_vip": vip,
        "is_admin": is_admin,
        "links_used": user["monthly_jobs_used"]
    }

@app.post("/process-video")
async def process_video_endpoint(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    email = normalize_email(body.get("email") or body.get("user_email"))
    url = str(body.get("video_url") or body.get("url") or body.get("link") or "").strip()

    if not email or not url:
        raise HTTPException(status_code=400, detail="Email සහ Video Link එක අනිවාර්යයි.")

    user = get_user(email)
    if not user:
        # User නොමැති නම් ස්වයංක්‍රීයව එක් කිරීම
        with db_connect() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO users (email, password_hash, is_vip, monthly_jobs_used, last_reset_month, created_at)
                VALUES (?, NULL, 0, 0, ?, ?)
            """, (email, current_month(), utcnow().isoformat()))
            conn.commit()
        user = get_user(email)

    is_vip = bool(user["is_vip"]) or (bool(ADMIN_EMAIL) and email == ADMIN_EMAIL)
    used = user["monthly_jobs_used"]

    if not is_vip and used >= FREE_MONTHLY_JOBS:
        raise HTTPException(status_code=403, detail="Free මාසික සීමාව (වීඩියෝ 15) අවසන් වී ඇත.")

    job_id = uuid.uuid4().hex
    now = utcnow().isoformat()
    with db_connect() as conn:
        conn.execute("UPDATE users SET monthly_jobs_used=monthly_jobs_used+1 WHERE email=?", (email,))
        conn.execute("""
            INSERT INTO jobs (job_id, email, source_url, status, progress, message, created_at, updated_at)
            VALUES (?, ?, ?, 'queued', 0, 'Queued', ?, ?)
        """, (job_id, email, url, now, now))
        conn.commit()

    return {
        "status": "success",
        "job_id": job_id,
        "message": "වීඩියෝ ක්‍රියාවලිය ආරම්භ විය."
    }

@app.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found.")

    res = {
        "job_id": row["job_id"],
        "status": row["status"],
        "progress": row["progress"],
        "message": row["message"],
        "error": row["error"]
    }
    if row["status"] == "completed" and row["results_json"]:
        res["results"] = json.loads(row["results_json"])
    return res

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
