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
            logger.warning("Could not remove temporary file: %s", path)

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
    if os.path.isabs(FFMPEG_BIN):
        ffmpeg_available = os.path.isfile(FFMPEG_BIN) and os.access(FFMPEG_BIN, os.X_OK)
    else:
        ffmpeg_available = shutil.which(FFMPEG_BIN) is not None

    return {
        "status": "ok",
        "ffmpeg_available": ffmpeg_available,
        "public_base_url_configured": bool(PUBLIC_BASE_URL),
        "database_configured": bool(DB_PATH),
    }

app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")

def generate_metadata_with_gemini(video_title: str, part_num: int, total_parts: int):
    clean_title = re.sub(r"[^\w\s-]", "", video_title or "Social Video")[:40].strip()
    if not clean_title:
        clean_title = "Social Video"

    suffix = f" (Part {part_num})" if total_parts > 1 else ""
    fallback_title = f"🔥 {clean_title}{suffix}"

    fallback_result = {
        "title": fallback_title,
        "description": f"Must see highlight clip from {clean_title}! Like and follow for more.",
        "hashtags": "#Shorts #Viral #Trending #FYP #Reels",
    }

    if not GEMINI_API_KEY:
        return fallback_result

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    prompt = f"""
Video topic: "{video_title}". Part {part_num} of {total_parts}.
Create a catchy social-video title under 45 characters ending with "{suffix}".
Respond ONLY in strict JSON:
{{"title": "Title with emoji{suffix}", "description": "Engaging description", "hashtags": "#Viral #Trending #FYP #Reels"}}
"""

    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            response_data = json.loads(response.read().decode("utf-8"))

        candidates = response_data.get("candidates") or []
        raw_text = candidates[0]["content"]["parts"][0]["text"]
        clean_text = raw_text.replace("```json", "").replace("```", "").strip()
        metadata = json.loads(clean_text)

        return {
            "title": str(metadata.get("title") or fallback_title)[:100],
            "description": str(metadata.get("description") or f"Highlight clip from {clean_title}.")[:1000],
            "hashtags": str(metadata.get("hashtags") or "#Shorts #Viral #Trending")[:500],
        }
    except Exception as exc:
        logger.warning("Gemini generation skipped (%s). Using fallback metadata.", exc)
        return fallback_result

@app.post("/auth/register")
async def register(request: Request):
    try:
        data = read_json_body(await request.json())
    except Exception:
        raise HTTPException(status_code=400, detail="Request එක වලංගු JSON එකක් නොවේ.")

    email = normalize_email(get_first_string(data, "email", "user_email"))
    password = get_first_string(data, "password")
    device_id = get_first_string(data, "device_id") or "UNKNOWN"

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="වලංගු Email ලිපිනයක් ඇතුළත් කරන්න.")
    if not password:
        raise HTTPException(status_code=400, detail="Password එක අවශ්‍යයි.")

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        current_month = datetime.now().strftime("%Y-%m")

        if email != normalize_email(ADMIN_EMAIL) and device_id != "UNKNOWN":
            cursor.execute("SELECT email FROM users WHERE device_id = ?", (device_id,))
            if cursor.fetchone():
                raise HTTPException(
                    status_code=403,
                    detail="මෙම දුරකථනයෙන් දැනටමත් Free ගිණුමක් ලියාපදිංචි කර ඇත!",
                )

        try:
            cursor.execute(
                """
                INSERT INTO users
                (email, password, is_vip, vip_expiry, monthly_clips_used, last_reset_month, device_id)
                VALUES (?, ?, 0, NULL, 0, ?, ?)
                """,
                (email, password, current_month, device_id),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="මෙම Email එක දැනටමත් ලියාපදිංචි කර ඇත.")

        return {"status": "success", "message": "ගිණුම සාදන ලදී!"}
    finally:
        conn.close()

@app.post("/auth/login")
async def login(request: Request):
    try:
        data = read_json_body(await request.json())
    except Exception:
        raise HTTPException(status_code=400, detail="Request එක වලංගු JSON එකක් නොවේ.")

    email = normalize_email(get_first_string(data, "email", "user_email"))
    password = get_first_string(data, "password")
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email සහ Password අවශ්‍යයි.")

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT email, password, is_vip, vip_expiry, monthly_clips_used
            FROM users WHERE LOWER(TRIM(email)) = ?
            """,
            (email,),
        )
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

    if is_vip and vip_expiry and not is_admin:
        try:
            if datetime.now().date() > datetime.strptime(vip_expiry, "%Y-%m-%d").date():
                is_vip = 0
        except ValueError:
            is_vip = 0

    return {
        "status": "success",
        "email": user_email,
        "is_vip": bool(is_vip),
        "is_admin": is_admin,
        "links_used": clips_used or 0,
    }

@app.post("/auth/forgot-password")
async def forgot_password(request: Request):
    try:
        data = read_json_body(await request.json())
    except Exception:
        raise HTTPException(status_code=400, detail="Request එක වලංගු JSON එකක් නොවේ.")

    email = normalize_email(get_first_string(data, "email", "user_email"))
    if not email:
        raise HTTPException(status_code=400, detail="Email ලිපිනය ඇතුළත් කරන්න.")

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM users WHERE LOWER(TRIM(email)) = ?", (email,))
        user = cursor.fetchone()
    finally:
        conn.close()

    if not user:
        raise HTTPException(status_code=404, detail="Email ලිපිනය හමු නොවීය.")

    otp = str(random.randint(100000, 999999))
    otp_storage[email] = {
        "otp": otp,
        "expires_at": datetime.now() + timedelta(minutes=10),
    }

    return {
        "status": "success",
        "message": f"Demo OTP එක ජනනය කරන ලදී (OTP: {otp})",
        "demo_otp": otp,
    }

@app.post("/auth/reset-password")
async def reset_password(request: Request):
    try:
        data = read_json_body(await request.json())
    except Exception:
        raise HTTPException(status_code=400, detail="Request එක වලංගු JSON එකක් නොවේ.")

    email = normalize_email(get_first_string(data, "email", "user_email"))
    otp = get_first_string(data, "otp")
    new_password = get_first_string(data, "new_password", "password")

    if not email or not otp or not new_password:
        raise HTTPException(status_code=400, detail="Email, OTP සහ අලුත් Password එක අවශ්‍යයි.")

    stored = otp_storage.get(email)
    if (
        not stored
        or stored.get("otp") != otp
        or datetime.now() > stored.get("expires_at", datetime.min)
    ):
        otp_storage.pop(email, None)
        raise HTTPException(status_code=400, detail="OTP අංකය වැරදියි හෝ කල් ඉකුත් වී ඇත.")

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET password = ? WHERE LOWER(TRIM(email)) = ?", (new_password, email))
        conn.commit()
    finally:
        conn.close()

    otp_storage.pop(email, None)
    return {"status": "success", "message": "Password යාවත්කාලීන විය!"}

@app.post("/admin/activate-vip")
async def activate_vip(request: Request):
    try:
        data = read_json_body(await request.json())
    except Exception:
        raise HTTPException(status_code=400, detail="Request එක වලංගු JSON එකක් නොවේ.")

    supplied_admin_email = normalize_email(get_first_string(data, "admin_email", "email"))
    target_user_email = normalize_email(get_first_string(data, "target_user_email", "user_email", "target_email"))

    if not ADMIN_EMAIL or supplied_admin_email != normalize_email(ADMIN_EMAIL):
        raise HTTPException(status_code=403, detail="Admin අවසර නැත.")

    if not target_user_email:
        raise HTTPException(status_code=400, detail="VIP සක්‍රීය කළ යුතු User Email එක ඇතුළත් කරන්න.")

    expiry = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET is_vip = 1, vip_expiry = ? WHERE LOWER(TRIM(email)) = ?",
            (expiry, target_user_email),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="මෙම Email එකට User කෙනෙක් හමු නොවීය.")
    finally:
        conn.close()

    return {
        "status": "success",
        "message": f"{target_user_email} VIP සක්‍රීය විය!",
        "vip_expiry": expiry,
    }

def download_social_video(url: str, output_path: str):
    clean_url = (url or "").strip()
    if not clean_url:
        raise HTTPException(status_code=400, detail="කරුණාකර video link එකක් ඇතුළත් කරන්න.")

    if not re.match(r"^https?://", clean_url, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="http:// හෝ https:// සහිත වලංගු link එකක් දෙන්න.")

    host = re.sub(r"^www\.", "", clean_url.split("/")[2].split(":")[0].lower())
    blocked_hosts = {"youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
    if host in blocked_hosts or host.endswith(".youtube.com"):
        raise HTTPException(
            status_code=400,
            detail="YouTube සහය නොදක්වයි. Facebook, TikTok, Instagram, X හෝ Pinterest link එකක් දෙන්න.",
        )

    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": output_path,
        "overwrites": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 35,
        "retries": 3,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=True)
            if not info:
                raise RuntimeError("No video information extracted")

            downloaded_path = ydl.prepare_filename(info)
            candidates = [
                output_path,
                os.path.splitext(output_path)[0] + ".mp4",
                os.path.splitext(downloaded_path)[0] + ".mp4",
                downloaded_path,
            ]
            actual_path = next((p for p in candidates if os.path.isfile(p)), None)
            if actual_path is None:
                raise RuntimeError("Downloaded video file was not found")

            if actual_path != output_path:
                shutil.move(actual_path, output_path)

            title = info.get("title") or "Viral Social Video"
            duration = max(1, int(info.get("duration") or 60))
            return title, duration

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Video download failed")
        raise HTTPException(
            status_code=502,
            detail=f"වීඩියෝව බාගත කිරීම අසාර්ථක විය: {str(exc)[:220]}",
        ) from exc

def escape_drawtext(text: str) -> str:
    text = str(text or "").replace("\n", " ").replace("\r", " ")
    replacements = {
        "\\": r"\\",
        "'": r"\'",
        ":": r"\:",
        "%": r"\%",
        "[": r"\[",
        "]": r"\]",
        ",": r"\,",
        ";": r"\;",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text[:35]

@app.post("/process-video")
async def process_video(request: Request):
    try:
        body = read_json_body(await request.json())
    except Exception:
        body = {}

    user_email = normalize_email(
        get_first_string(body, "email", "user_email", "userEmail")
    )
    url_to_process = get_first_string(
        body,
        "video_url",
        "youtube_url",
        "url",
        "social_video_url",
        "videoUrl",
        "link",
    )

    if not user_email:
        raise HTTPException(
            status_code=400,
            detail="User email එක අවශ්‍යයි. Request JSON එකට email field එක එක් කරන්න.",
        )
    if not url_to_process:
        raise HTTPException(
            status_code=400,
            detail="Video URL එක අවශ්‍යයි. video_url field එකට link එක යවන්න.",
        )

    conn = get_db_connection()
    raw_video = os.path.join(
        PROJECT_DIR, f"temp_{int(datetime.now().timestamp() * 1000)}.mp4"
    )
    output_data = []

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT is_vip, vip_expiry, monthly_clips_used, last_reset_month
            FROM users WHERE LOWER(TRIM(email)) = ?
            """,
            (user_email,),
        )
        user = cursor.fetchone()

        is_admin_email = bool(ADMIN_EMAIL) and user_email == normalize_email(ADMIN_EMAIL)

        if not user:
            if not is_admin_email:
                raise HTTPException(
                    status_code=401,
                    detail="මෙම Email එක ලියාපදිංචි වී නැත. පළමුව Register/Login වන්න.",
                )
            cursor.execute(
                """
                INSERT OR IGNORE INTO users
                (email, password, is_vip, vip_expiry, monthly_clips_used, last_reset_month, device_id)
                VALUES (?, NULL, 1, '2099-12-31', 0, ?, 'ADMIN_DEVICE')
                """,
                (user_email, datetime.now().strftime("%Y-%m")),
            )
            conn.commit()
            cursor.execute(
                """
                SELECT is_vip, vip_expiry, monthly_clips_used, last_reset_month
                FROM users WHERE LOWER(TRIM(email)) = ?
                """,
                (user_email,),
            )
            user = cursor.fetchone()

        is_vip, vip_expiry, clips_used, last_reset_month = user
        clips_used = int(clips_used or 0)
        current_month = datetime.now().strftime("%Y-%m")

        if is_admin_email:
            is_vip = 1
            vip_expiry = "2099-12-31"

        if last_reset_month != current_month:
            clips_used = 0
            cursor.execute(
                "UPDATE users SET monthly_clips_used = 0, last_reset_month = ? WHERE LOWER(TRIM(email)) = ?",
                (current_month, user_email),
            )
            conn.commit()

        if is_vip and vip_expiry and not is_admin_email:
            try:
                if datetime.now().date() > datetime.strptime(vip_expiry, "%Y-%m-%d").date():
                    is_vip = 0
                    cursor.execute(
                        "UPDATE users SET is_vip = 0 WHERE LOWER(TRIM(email)) = ?", (user_email,)
                    )
                    conn.commit()
            except ValueError:
                is_vip = 0

        if not is_vip and clips_used >= 15:
            raise HTTPException(
                status_code=403,
                detail=f"Free මාසික සීමාව (වීඩියෝ 15) අවසන්! (දැනට: {clips_used}). PRO VIP ලබාගන්න.",
            )

        video_title, total_duration = download_social_video(url_to_process, raw_video)

        if not is_vip and total_duration > 600:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"මෙම වීඩියෝව විනාඩි {math.ceil(total_duration / 60)} කි. "
                    "Free Plan එකේ උපරිම විනාඩි 10 ක වීඩියෝ පමණි."
                ),
            )

        part_duration = 180
        total_parts = max(1, math.ceil(total_duration / part_duration))
        parts_to_generate = total_parts

        if not is_vip:
            remaining_quota = max(0, 15 - clips_used)
            parts_to_generate = min(total_parts, remaining_quota)

        for i in range(parts_to_generate):
            start_sec = i * part_duration
            current_clip_len = min(part_duration, total_duration - start_sec)
            if current_clip_len <= 3:
                continue

            clip_name = f"repurpose_{int(datetime.now().timestamp())}_{i + 1}_{random.randint(1000,9999)}.mp4"
            final_path = os.path.join(OUTPUT_DIR, clip_name)

            meta = generate_metadata_with_gemini(video_title, i + 1, total_parts)
            title_text = escape_drawtext(meta.get("title", ""))

            base_filter = (
                "[0:v]scale=720:1280:force_original_aspect_ratio=increase,"
                "crop=720:1280,boxblur=15:5[bg];"
                "[0:v]scale=720:-1,setsar=1,"
                "eq=brightness=0.03:saturation=1.12:contrast=1.05,"
                "setpts=0.97*PTS[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2[combined];"
                f"[combined]drawtext=text='{title_text}':"
                "fontcolor=yellow:fontsize=36:box=1:boxcolor=black@0.65:"
                "boxborderw=10:x=(w-text_w)/2:y=120[vout]"
            )

            # VIP / Admin අයට Watermark එක 100% ක් සම්පූර්ණයෙන්ම ඉවත් වේ
            if not is_vip:
                vf_filter = (
                    base_filter
                    + ";[vout]drawtext=text='ShortsFlow AI':"
                    "x=w-tw-25:y=h-th-40:fontsize=22:"
                    "fontcolor=white@0.55:shadowcolor=black@0.4:"
                    "shadowx=2:shadowy=2[vfinal]"
                )
                video_map = "[vfinal]"
            else:
                vf_filter = base_filter
                video_map = "[vout]"

            command = [
                FFMPEG_BIN,
                "-y",
                "-ss",
                str(start_sec),
                "-i",
                raw_video,
                "-t",
                str(int(current_clip_len)),
                "-filter_complex",
                vf_filter,
                "-map",
                video_map,
                "-map",
                "0:a?",
                "-af",
                "atempo=1.03,volume=1.05",
                "-c:v",
                "libx264",
                "-crf",
                "22",
                "-preset",
                "ultrafast",  # ක්ෂණිකව convert වීමට ultrafast සකසා ඇත
                "-threads",
                "2",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-map_metadata",
                "-1",
                final_path,
            ]

            try:
                subprocess.run(
                    command,
                    check=True,
                    timeout=240,
                    capture_output=True,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                safe_remove(final_path)
                raise HTTPException(
                    status_code=504,
                    detail="වීඩියෝ සැකසීමට වැඩි කාලයක් ගත විය. කෙටි වීඩියෝවක් උත්සාහ කරන්න.",
                )
            except subprocess.CalledProcessError as exc:
                safe_remove(final_path)
                error_text = (exc.stderr or exc.stdout or str(exc)).strip()
                logger.error("FFmpeg failed: %s", error_text[-1500:])
                raise HTTPException(
                    status_code=500,
                    detail=f"FFmpeg error: {error_text[-350:]}",
                )

            output_data.append(
                {
                    "clip_url": f"{PUBLIC_BASE_URL}/output/{clip_name}",
                    "title": meta.get("title", ""),
                    "description": meta.get("description", ""),
                    "hashtags": meta.get("hashtags", ""),
                }
            )

        if not output_data:
            raise HTTPException(
                status_code=400,
                detail="සැකසීමට ප්‍රමාණවත් දිගක වීඩියෝ කොටසක් හමු නොවීය.",
            )

        if not is_vip:
            cursor.execute(
                """
                UPDATE users
                SET monthly_clips_used = COALESCE(monthly_clips_used, 0) + ?,
                    last_reset_month = ?
                WHERE LOWER(TRIM(email)) = ?
                """,
                (len(output_data), current_month, user_email),
            )
            conn.commit()

        return {"status": "success", "results": output_data}

    except HTTPException:
        raise
    except sqlite3.Error as exc:
        logger.exception("Database error during video processing")
        raise HTTPException(status_code=500, detail="Database error. Server logs පරීක්ෂා කරන්න.") from exc
    except Exception as exc:
        logger.exception("Unexpected process-video error")
        raise HTTPException(
            status_code=500,
            detail=f"වීඩියෝව සැකසීමේදී අනපේක්ෂිත දෝෂයක් ඇති විය: {str(exc)[:200]}",
        ) from exc
    finally:
        safe_remove(raw_video)
        conn.close()

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
    )
