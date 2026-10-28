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
from pydantic import BaseModel, Field

# ShortsFlow AI backend
# Use only videos you own or are authorized to download and republish.
# This app does not remove watermarks or guarantee copyright clearance.

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

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().casefold()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
FREE_MONTHLY_JOBS = int(os.getenv("FREE_MONTHLY_JOBS", "15"))
FREE_MAX_DURATION_SECONDS = int(os.getenv("FREE_MAX_DURATION_SECONDS", "600"))
PART_DURATION_SECONDS = int(os.getenv("PART_DURATION_SECONDS", "180"))
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1")))
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "1500"))
JOB_RETENTION_DAYS = int(os.getenv("JOB_RETENTION_DAYS", "3"))
PORT = int(os.getenv("PORT", "8000"))

SUPPORTED_HOSTS = (
    "tiktok.com", "facebook.com", "fb.watch", "instagram.com",
    "x.com", "twitter.com", "pinterest.com", "pin.it"
)

app = FastAPI(title=APP_NAME, version="2.0.0")
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")

# Runtime-only OTP store. For production, connect an email provider and use a persistent,
# expiring OTP store. This demo intentionally does not expose OTPs in API responses.
otp_storage: dict[str, dict[str, Any]] = {}
worker_task = None
worker_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def normalize_email(value: Any) -> str:
    return str(value or "").strip().casefold()


def db_connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
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
            updated_at TEXT NOT NULL,
            FOREIGN KEY(email) REFERENCES users(email)
        )
        """)
        # Create/upgrade a legacy database with the original plaintext password column.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "password_hash" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        if "created_at" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN created_at TEXT")
        if "monthly_jobs_used" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN monthly_jobs_used INTEGER NOT NULL DEFAULT 0")
        if "last_reset_month" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_reset_month TEXT")
        if "device_id" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN device_id TEXT")
        # Migrate the user's legacy plaintext password column, if present.
        if "password" in columns:
            rows = conn.execute("SELECT email, password, password_hash FROM users").fetchall()
            for row in rows:
                if not row["password_hash"] and row["password"]:
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE email = ?",
                        (hash_password(str(row["password"])), row["email"])
                    )
        if ADMIN_EMAIL and ADMIN_PASSWORD:
            existing = conn.execute(
                "SELECT email FROM users WHERE lower(trim(email)) = ?", (ADMIN_EMAIL,)
            ).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO users
                    (email, password_hash, is_vip, vip_expiry, monthly_jobs_used,
                     last_reset_month, device_id, created_at)
                    VALUES (?, ?, 1, '2099-12-31', 0, ?, 'ADMIN_DEVICE', ?)
                """, (ADMIN_EMAIL, hash_password(ADMIN_PASSWORD), current_month(), utcnow().isoformat()))
            else:
                conn.execute(
                    "UPDATE users SET is_vip=1, vip_expiry='2099-12-31' WHERE lower(trim(email))=?",
                    (ADMIN_EMAIL,)
                )
        # Requeue jobs interrupted by a container restart.
        conn.execute("""
            UPDATE jobs SET status='queued', message='Requeued after service restart',
            updated_at=? WHERE status='processing'
        """, (utcnow().isoformat(),))


def hash_password(password: str) -> str:
    # PBKDF2 with per-password random salt.
    import hashlib
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240_000)
    return "pbkdf2_sha256$240000$" + salt.hex() + "$" + digest.hex()


def verify_password(password: str, encoded: str) -> bool:
    import hashlib
    try:
        scheme, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            # Compatibility with old plaintext records, migrated where possible.
            return secrets.compare_digest(password, encoded)
        calculated = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return secrets.compare_digest(calculated.hex(), digest_hex)
    except Exception:
        return False


init_db()


def get_user(email: str):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE lower(trim(email))=?", (normalize_email(email),)
        ).fetchone()


def update_job(job_id: str, **fields):
    if not fields:
        return
    fields["updated_at"] = utcnow().isoformat()
    allowed = {"status", "progress", "message", "results_json", "error", "updated_at"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    assignments = ", ".join(f"{key}=?" for key in fields)
    with db_connect() as conn:
        conn.execute(
            f"UPDATE jobs SET {assignments} WHERE job_id=?",
            [*fields.values(), job_id]
        )


def valid_source_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname.casefold()
        return any(host == domain or host.endswith("." + domain) for domain in SUPPORTED_HOSTS)
    except Exception:
        return False


def public_file_url(filename: str) -> str:
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/output/{filename}"
    return f"/output/{filename}"


def clean_title(value: str, limit: int = 90) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    value = re.sub(r"[\x00-\x1f]", "", value)
    return value[:limit] or "Video Highlight"


def generate_metadata(video_title: str, part_num: int, total_parts: int, duration: int) -> dict:
    title = clean_title(video_title, 140)
    suffix = f" | Part {part_num}" if total_parts > 1 else ""
    fallback = {
        "title": clean_title(title, 65) + suffix,
        "description": f"Watch this {part_num} highlight from {title}. Share your thoughts and follow for more.",
        "hashtags": "#Shorts #VideoHighlights #Trending"
    }
    if not GEMINI_API_KEY:
        return fallback

    prompt = f"""
You write accurate social-media metadata for a video clip.
Original video title: {title}
Clip number: {part_num} of {total_parts}
Clip duration in seconds: {duration}
Return ONLY valid JSON with exactly these string keys:
"title" (catchy, accurate, max 80 characters; include Part {part_num} if there are multiple clips),
"description" (1-2 engaging sentences, do not invent facts not present in the title),
"hashtags" (5-8 relevant hashtags, space-separated).
Do not claim the video is copyright-free, do not impersonate anyone, and do not use misleading claims.
"""
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    try:
        response = requests.post(
            endpoint,
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.65, "responseMimeType": "application/json"}
            },
            timeout=(5, 25)
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(raw)
        return {
            "title": clean_title(parsed.get("title") or fallback["title"], 100),
            "description": clean_title(parsed.get("description") or fallback["description"], 500),
            "hashtags": clean_title(parsed.get("hashtags") or fallback["hashtags"], 300)
        }
    except Exception as exc:
        logger.warning("Gemini metadata generation failed; using fallback: %s", exc)
        return fallback


def run_command(args: list[str], timeout: int):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)


def download_video(url: str, job_id: str) -> tuple[Path, str, int]:
    template = str(TEMP_DIR / f"{job_id}.%(ext)s")
    opts = {
        "format": "bestvideo*+bestaudio/best",
        "outtmpl": template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "continuedl": True,
        "overwrites": True,
        "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
        }
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("The platform did not return video information.")
        title = clean_title(info.get("title") or "Video Highlight", 140)
        duration = int(info.get("duration") or 0)
        prepared = Path(ydl.prepare_filename(info))
    candidates = [prepared, prepared.with_suffix(".mp4"), prepared.with_suffix(".mkv"),
                  prepared.with_suffix(".webm"), prepared.with_suffix(".mov")]
    found = next((p for p in candidates if p.exists() and p.is_file()), None)
    if not found:
        matches = list(TEMP_DIR.glob(f"{job_id}.*"))
        found = next((p for p in matches if p.is_file() and p.suffix not in (".part", ".ytdl")), None)
    if not found or not found.exists():
        raise RuntimeError("Downloaded video file could not be found.")
    # Trust ffprobe for actual duration where possible.
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(found)],
            capture_output=True, text=True, timeout=20, check=True
        )
        actual = float(probe.stdout.strip())
        if actual > 0:
            duration = int(actual + 0.5)
    except Exception:
        pass
    if duration <= 0:
        raise RuntimeError("Could not determine the video duration.")
    return found, title, duration


def split_video(source: Path, job_id: str, title: str, duration: int) -> list[dict]:
    total_parts = (duration + PART_DURATION_SECONDS - 1) // PART_DURATION_SECONDS
    results = []
    for index in range(total_parts):
        start = index * PART_DURATION_SECONDS
        clip_duration = min(PART_DURATION_SECONDS, duration - start)
        if clip_duration <= 0:
            continue
        output_name = f"{job_id}_part_{index + 1:03d}.mp4"
        output_path = OUTPUT_DIR / output_name
        update_job(job_id, progress=min(85, 20 + int(65 * index / max(total_parts, 1))),
                   message=f"Creating clip {index + 1} of {total_parts}")
        # Re-encode for reliable segment boundaries and broadly compatible MP4 output.
        # Vertical crop is intentionally not forced: preserving source framing avoids
        # cutting off content. App can add an optional 9:16 rendering mode later.
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(start), "-i", str(source), "-t", str(clip_duration),
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", "scale='min(1080,iw)':-2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            "-map_metadata", "-1", str(output_path)
        ]
        try:
            run_command(command, timeout=max(180, clip_duration * 8))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"FFmpeg timed out while creating clip {index + 1}. Try a smaller video or a larger Koyeb instance.") from exc
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or str(exc))[-1200:]
            raise RuntimeError(f"FFmpeg failed on clip {index + 1}: {details}") from exc
        meta = generate_metadata(title, index + 1, total_parts, clip_duration)
        results.append({
            "part": index + 1,
            "start_seconds": start,
            "duration_seconds": clip_duration,
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
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user:
        update_job(job_id, status="failed", error="User account not found.", message="Failed")
        return

    is_vip = bool(user["is_vip"])
    if is_vip and user["vip_expiry"]:
        try:
            is_vip = datetime.strptime(user["vip_expiry"], "%Y-%m-%d").date() >= datetime.now().date()
        except ValueError:
            is_vip = False

    source_path = None
    try:
        update_job(job_id, status="processing", progress=5, message="Downloading video")
        source_path, title, duration = download_video(url, job_id)
        if not is_vip and duration > FREE_MAX_DURATION_SECONDS:
            raise RuntimeError(
                f"Free plan videos are limited to {FREE_MAX_DURATION_SECONDS // 60} minutes. VIP is required for longer videos."
            )
        update_job(job_id, progress=15, message="Preparing video clips")
        results = split_video(source_path, job_id, title, duration)
        update_job(job_id, status="completed", progress=100, message="Completed",
                   results_json=json.dumps(results, ensure_ascii=False), error=None)
        logger.info("Job %s completed with %s clips", job_id, len(results))
    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        update_job(job_id, status="failed", progress=100, message="Processing failed", error=str(exc)[:1800])
    finally:
        for path in TEMP_DIR.glob(f"{job_id}.*"):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


async def worker_loop():
    logger.info("Background worker started")
    while True:
        try:
            async with worker_semaphore:
                with db_connect() as conn:
                    row = conn.execute(
                        "SELECT job_id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
                    ).fetchone()
                    if row:
                        job_id = row["job_id"]
                        # Mark as processing before leaving the database transaction.
                        conn.execute(
                            "UPDATE jobs SET status='processing', message='Starting', updated_at=? WHERE job_id=? AND status='queued'",
                            (utcnow().isoformat(), job_id)
                        )
                    else:
                        job_id = None
                if job_id:
                    await asyncio.to_thread(process_job, job_id)
                else:
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker loop error")
            await asyncio.sleep(3)


@app.on_event("startup")
async def startup_event():
    global worker_task
    worker_task = asyncio.create_task(worker_loop())


@app.on_event("shutdown")
async def shutdown_event():
    global worker_task
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


@app.get("/")
def root():
    return {"service": APP_NAME, "status": "online", "docs": "/docs"}


@app.get("/health")
def health():
    ffmpeg_ok = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
    with db_connect() as conn:
        queued = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status='queued'").fetchone()["n"]
        processing = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status='processing'").fetchone()["n"]
    return {
        "status": "ok" if ffmpeg_ok else "degraded",
        "ffmpeg_available": ffmpeg_ok,
        "gemini_configured": bool(GEMINI_API_KEY),
        "queued_jobs": queued,
        "processing_jobs": processing
    }


@app.post("/auth/register")
async def register(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    email = normalize_email(data.get("email") or data.get("user_email"))
    password = str(data.get("password") or "")
    device_id = str(data.get("device_id") or "UNKNOWN")[:200]
    if not email or "@" not in email or not password:
        raise HTTPException(status_code=400, detail="Valid email and password are required.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must contain at least 8 characters.")
    with db_connect() as conn:
        try:
            conn.execute("""
                INSERT INTO users
                (email, password_hash, is_vip, vip_expiry, monthly_jobs_used, last_reset_month, device_id, created_at)
                VALUES (?, ?, 0, NULL, 0, ?, ?, ?)
            """, (email, hash_password(password), current_month(), device_id, utcnow().isoformat()))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="This email is already registered.")
    return {"status": "success", "message": "Account created."}


@app.post("/auth/login")
async def login(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    email = normalize_email(data.get("email") or data.get("user_email"))
    password = str(data.get("password") or "")
    user = get_user(email)
    if not user or not user["password_hash"] or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    month = current_month()
    with db_connect() as conn:
        if user["last_reset_month"] != month:
            conn.execute("UPDATE users SET monthly_jobs_used=0, last_reset_month=? WHERE email=?", (month, email))
            used = 0
        else:
            used = user["monthly_jobs_used"] or 0
    is_admin = bool(ADMIN_EMAIL) and email == ADMIN_EMAIL
    vip = bool(user["is_vip"])
    expiry = user["vip_expiry"]
    if vip and expiry:
        try:
            vip = datetime.strptime(expiry, "%Y-%m-%d").date() >= datetime.now().date()
        except ValueError:
            vip = False
    if is_admin:
        vip = True
    return {
        "status": "success", "email": email, "is_vip": vip,
        "is_admin": is_admin, "links_used": used,
        "monthly_limit": None if vip else FREE_MONTHLY_JOBS
    }


@app.post("/auth/forgot-password")
async def forgot_password(request: Request):
    # Configure a real email provider before enabling password recovery in production.
    try:
        data = await request.json()
    except Exception:
        data = {}
    email = normalize_email(data.get("email"))
    if not email or not get_user(email):
        # Avoid exposing whether an account exists.
        return {"status": "success", "message": "If the account exists, password recovery instructions will be sent."}
    otp = f"{secrets.randbelow(1_000_000):06d}"
    otp_storage[email] = {"otp": otp, "expires": time.time() + 600}
    # OTP is not returned to the client and no email is sent by this template.
    # Add SMTP/provider delivery before using this endpoint for real recovery.
    logger.warning("Password recovery requested for %s; email delivery is not configured.", email)
    return {"status": "error", "message": "Password recovery email is not configured. Contact the administrator."}


@app.post("/auth/reset-password")
async def reset_password(request: Request):
    raise HTTPException(status_code=501, detail="Password reset is disabled until a verified email delivery provider is configured.")


@app.post("/admin/activate-vip")
async def activate_vip(request: Request, x_admin_password: str | None = Header(default=None)):
    if not ADMIN_EMAIL or not ADMIN_PASSWORD or not x_admin_password:
        raise HTTPException(status_code=403, detail="Admin authentication required.")
    if not secrets.compare_digest(x_admin_password, ADMIN_PASSWORD):
        raise HTTPException(status_code=403, detail="Invalid admin credentials.")
    try:
        data = await request.json()
    except Exception:
        data = {}
    target_email = normalize_email(data.get("target_user_email") or data.get("user_email"))
    if not target_email:
        raise HTTPException(status_code=400, detail="Target user email is required.")
    expiry = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    with db_connect() as conn:
        cur = conn.execute("UPDATE users SET is_vip=1, vip_expiry=? WHERE email=?", (expiry, target_email))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found.")
    return {"status": "success", "message": f"VIP activated for 30 days.", "vip_expiry": expiry}


@app.post("/process-video")
async def process_video(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    email = normalize_email(data.get("email") or data.get("user_email"))
    url = str(data.get("video_url") or data.get("url") or data.get("link") or "").strip()
    if not email or not url:
        raise HTTPException(status_code=400, detail="Email and video URL are required.")
    if not valid_source_url(url):
        raise HTTPException(
            status_code=400,
            detail="Supported sources: TikTok, Facebook, Instagram, X/Twitter, and Pinterest. YouTube is not supported."
        )
    user = get_user(email)
    if not user:
        raise HTTPException(status_code=404, detail="User account not found. Please register or log in.")
    is_admin = bool(ADMIN_EMAIL) and email == ADMIN_EMAIL
    is_vip = bool(user["is_vip"]) or is_admin
    if is_vip and user["vip_expiry"] and not is_admin:
        try:
            is_vip = datetime.strptime(user["vip_expiry"], "%Y-%m-%d").date() >= datetime.now().date()
        except ValueError:
            is_vip = False
    month = current_month()
    with db_connect() as conn:
        if user["last_reset_month"] != month:
            conn.execute("UPDATE users SET monthly_jobs_used=0, last_reset_month=? WHERE email=?", (month, email))
            used = 0
        else:
            used = user["monthly_jobs_used"] or 0
        if not is_vip and used >= FREE_MONTHLY_JOBS:
            raise HTTPException(status_code=403, detail=f"Monthly free limit of {FREE_MONTHLY_JOBS} jobs reached. VIP required.")
        # Count one job at submission, preventing parallel requests bypassing the quota.
        conn.execute("UPDATE users SET monthly_jobs_used=monthly_jobs_used+1 WHERE email=?", (email,))
        job_id = uuid.uuid4().hex
        now = utcnow().isoformat()
        conn.execute("""
            INSERT INTO jobs (job_id, email, source_url, status, progress, message, created_at, updated_at)
            VALUES (?, ?, ?, 'queued', 0, 'Queued for processing', ?, ?)
        """, (job_id, email, url, now, now))
    return {
        "status": "success",
        "job_id": job_id,
        "job_status_url": f"/jobs/{job_id}",
        "message": "Video job queued. Poll the job status endpoint until completed."
    }


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    email = normalize_email(request.query_params.get("email"))
    if not email:
        raise HTTPException(status_code=400, detail="Email query parameter is required.")
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=? AND email=?", (job_id, email)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found.")
    response = {
        "status": row["status"],
        "job_id": row["job_id"],
        "progress": row["progress"],
        "message": row["message"],
        "error": row["error"]
    }
    if row["status"] == "completed" and row["results_json"]:
        response["results"] = json.loads(row["results_json"])
    return response


@app.get("/jobs/{job_id}/results")
async def get_job_results(job_id: str, request: Request):
    email = normalize_email(request.query_params.get("email"))
    if not email:
        raise HTTPException(status_code=400, detail="Email query parameter is required.")
    with db_connect() as conn:
        row = conn.execute("SELECT status, results_json, error FROM jobs WHERE job_id=? AND email=?", (job_id, email)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found.")
    if row["status"] != "completed":
        return {"status": row["status"], "message": "Results are not ready yet.", "error": row["error"]}
    return {"status": "success", "results": json.loads(row["results_json"] or "[]")}


@app.get("/admin/jobs")
async def admin_jobs(x_admin_password: str | None = Header(default=None), limit: int = 50):
    if not ADMIN_PASSWORD or not x_admin_password or not secrets.compare_digest(x_admin_password, ADMIN_PASSWORD):
        raise HTTPException(status_code=403, detail="Admin authentication required.")
    limit = max(1, min(limit, 200))
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT job_id,email,status,progress,message,error,created_at,updated_at FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return {"jobs": [dict(row) for row in rows]}


@app.on_event("startup")
async def cleanup_old_files():
    # Remove old output files and completed/failed job records after retention.
    cutoff = utcnow() - timedelta(days=JOB_RETENTION_DAYS)
    try:
        with db_connect() as conn:
            old = conn.execute(
                "SELECT job_id FROM jobs WHERE updated_at < ? AND status IN ('completed','failed')",
                (cutoff.isoformat(),)
            ).fetchall()
            for row in old:
                for p in OUTPUT_DIR.glob(f"{row['job_id']}_part_*.mp4"):
                    p.unlink(missing_ok=True)
                conn.execute("DELETE FROM jobs WHERE job_id=?", (row["job_id"],))
    except Exception:
        logger.exception("Old job cleanup failed")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, workers=1)
