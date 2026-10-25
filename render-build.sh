import os
import re
import sqlite3
import subprocess
import shutil
import json
import random
import urllib.request
import math
from datetime import datetime, timedelta
from typing import Optional, Any
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="ShortsFlow Universal AI Backend")

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://shortsflow-server.onrender.com"
).strip().rstrip("/")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.environ.get(
    "SHORTSFLOW_DB_PATH", os.path.join(PROJECT_DIR, "shortsflow.db")
).strip()
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.abspath(os.path.join(PROJECT_DIR, DB_PATH))

_LEGACY_DB_PATH = os.path.abspath("shortsflow.db")
if (
    not os.path.exists(DB_PATH)
    and os.path.isfile(_LEGACY_DB_PATH)
    and os.path.abspath(_LEGACY_DB_PATH) != os.path.abspath(DB_PATH)
):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    shutil.copy2(_LEGACY_DB_PATH, DB_PATH)

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def normalize_email(email: str) -> str:
    return (email or "").strip().casefold()

OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

FFMPEG_DIR = "/opt/render/project/.render/ffmpeg"
FFMPEG_BIN = os.path.join(FFMPEG_DIR, "ffmpeg")
if not (os.path.isfile(FFMPEG_BIN) and os.access(FFMPEG_BIN, os.X_OK)):
    FFMPEG_BIN = "ffmpeg"

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
    }

app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "anushkamalshan666@gmail.com").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "@Anushka*malshan2005")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

otp_storage = {}

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
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
    conn.commit()
    conn.close()

init_db()

def generate_metadata_with_gemini(video_title: str, part_num: int, total_parts: int):
    clean_title = re.sub(r'[^\w\s-]', '', video_title)[:40].strip()
    suffix = f" (Part {part_num})" if total_parts > 1 else ""
    fallback_title = f"🔥 {clean_title}{suffix}"

    if not GEMINI_API_KEY:
        return {
            "title": fallback_title,
            "description": f"Highlight clip from {clean_title}! Share, like and follow.",
            "hashtags": "#Shorts #Viral #Trending #FYP #Reels"
        }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = f"""
    Video topic: "{video_title}". Part {part_num} of {total_parts}.
    Create a viral Title (under 45 chars) with an emoji ending with "{suffix}".
    Respond ONLY in strict JSON:
    {{
        "title": "Dramatic Title{suffix}",
        "description": "Engaging 2-sentence description",
        "hashtags": "#Viral #Trending #FYP #Reels #Shorts"
    }}
    """
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            raw_text = res_data['candidates'][0]['content']['parts'][0]['text']
            clean_text = raw_text.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_text)
    except Exception:
        return {
            "title": fallback_title,
            "description": f"Must see clip from {clean_title}!",
            "hashtags": "#Viral #Trending #FYP #Reels"
        }

class AuthReq(BaseModel):
    email: str
    password: str
    device_id: str = "UNKNOWN"

class ForgotReq(BaseModel):
    email: str

class ResetReq(BaseModel):
    email: str
    otp: str
    new_password: str

class ActivateVipReq(BaseModel):
    admin_email: str
    target_user_email: str

# කිසිදු field එකක් missing නොවන flexible schema එකක්
class ProcessVideoReq(BaseModel):
    email: str
    video_url: Optional[str] = None
    youtube_url: Optional[str] = None
    clip_count: Optional[int] = 1

@app.post("/auth/register")
def register(req: AuthReq):
    conn = get_db_connection()
    cursor = conn.cursor()
    current_month = datetime.now().strftime("%Y-%m")
    user_email = normalize_email(req.email)
    
    if user_email != ADMIN_EMAIL.lower() and req.device_id and req.device_id != "UNKNOWN":
        cursor.execute("SELECT email FROM users WHERE device_id = ?", (req.device_id,))
        if cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=403, detail="මෙම දුරකථනයෙන් දැනටමත් Free ගිණුමක් ලියාපදිංචි කර ඇත!")

    try:
        cursor.execute(
            "INSERT INTO users (email, password, is_vip, vip_expiry, monthly_clips_used, last_reset_month, device_id) "
            "VALUES (?, ?, 0, NULL, 0, ?, ?)",
            (user_email, req.password, current_month, req.device_id)
        )
        conn.commit()
        return {"status": "success", "message": "ගිණුම සාදන ලදී!"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="මෙම Email එක දැනටමත් ලියාපදිංචි කර ඇත.")
    finally:
        conn.close()

@app.post("/auth/login")
def login(req: AuthReq):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT email, is_vip, vip_expiry, monthly_clips_used FROM users WHERE LOWER(TRIM(email)) = ? AND password = ?", 
                   (normalize_email(req.email), req.password))
    user = cursor.fetchone()
    conn.close()
    
    if not user:
        raise HTTPException(status_code=401, detail="Email හෝ Password වැරදියි.")
    
    email, is_vip, vip_expiry, clips_used = user
    if is_vip and vip_expiry and datetime.now() > datetime.strptime(vip_expiry, "%Y-%m-%d"):
        is_vip = 0
            
    return {
        "status": "success",
        "email": email,
        "is_vip": bool(is_vip),
        "is_admin": (normalize_email(email) == normalize_email(ADMIN_EMAIL) if ADMIN_EMAIL else False),
        "links_used": clips_used
    }

@app.post("/auth/forgot-password")
def forgot_password(req: ForgotReq):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT email FROM users WHERE LOWER(TRIM(email)) = ?", (normalize_email(req.email),))
    user = cursor.fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=404, detail="Email ලිපිනය හමු නොවීය.")

    otp = str(random.randint(100000, 999999))
    otp_storage[normalize_email(req.email)] = otp
    return {"status": "success", "message": "OTP කේතය Email වෙත යවන ලදී (Demo: " + otp + ")"}

@app.post("/auth/reset-password")
def reset_password(req: ResetReq):
    target_email = normalize_email(req.email)
    stored_otp = otp_storage.get(target_email)

    if not stored_otp or stored_otp != req.otp.strip():
        raise HTTPException(status_code=400, detail="OTP අංකය වැරදියි.")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET password = ? WHERE email = ?", (req.new_password, target_email))
    conn.commit()
    conn.close()

    del otp_storage[target_email]
    return {"status": "success", "message": "Password යාවත්කාලීන විය!"}

@app.post("/admin/activate-vip")
def activate_vip(req: ActivateVipReq):
    if not ADMIN_EMAIL or not ADMIN_PASSWORD or normalize_email(req.admin_email) != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="අවසර නැත.")
    
    expiry = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_vip = 1, vip_expiry = ? WHERE email = ?", 
                   (expiry, normalize_email(req.target_user_email)))
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"{req.target_user_email} VIP සක්‍රීය විය!"}

def download_social_video(url: str, output_path: str):
    clean_url = (url or "").strip()
    if not clean_url:
        raise HTTPException(status_code=400, detail="කරුණාකර video link එකක් ඇතුළත් කරන්න.")

    if "youtube.com" in clean_url or "youtu.be" in clean_url:
        raise HTTPException(
            status_code=400, 
            detail="YouTube සහය නොදක්වයි. කරුණාකර Facebook, TikTok, Instagram, Twitter (X), Pinterest වීඩියෝ link එකක් ලබා දෙන්න."
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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=True)
            if not info:
                raise RuntimeError("No video information extracted")
            title = info.get("title") or "Viral Video"
            duration = int(info.get("duration") or 60)
            return title, duration
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"වීඩියෝව බාගත කිරීම අසාර්ථක විය: {str(exc)[:250]}"
        ) from exc

@app.post("/process-video")
def process_video(req: ProcessVideoReq):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_vip, monthly_clips_used FROM users WHERE LOWER(TRIM(email)) = ?", (normalize_email(req.email),))
    user = cursor.fetchone()
    
    if not user:
        email_key = normalize_email(req.email)
        if not email_key or "@" not in email_key:
            conn.close()
            raise HTTPException(status_code=400, detail="වලංගු email ලිපිනයක් අවශ්‍යයි.")
        is_admin_email = bool(ADMIN_EMAIL) and email_key == normalize_email(ADMIN_EMAIL)
        try:
            cursor.execute(
                "INSERT OR IGNORE INTO users "
                "(email, password, is_vip, vip_expiry, monthly_clips_used, last_reset_month, device_id) "
                "VALUES (?, NULL, ?, ?, 0, ?, ?)",
                (
                    email_key,
                    1 if is_admin_email else 0,
                    "2099-12-31" if is_admin_email else None,
                    datetime.now().strftime("%Y-%m"),
                    "ADMIN_DEVICE" if is_admin_email else None,
                ),
            )
            conn.commit()
            cursor.execute("SELECT is_vip, monthly_clips_used FROM users WHERE LOWER(TRIM(email)) = ?", (email_key,))
            user = cursor.fetchone()
        except sqlite3.Error as exc:
            conn.close()
            raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    is_vip, clips_used = user[0], user[1] or 0

    if not is_vip and clips_used >= 15:
        conn.close()
        raise HTTPException(
            status_code=403, 
            detail=f"Free මාසික සීමාව (වීඩියෝ 15) අවසන්! (දැනට: {clips_used}). PRO VIP ලබාගන්න."
        )

    # youtube_url හෝ video_url දෙකෙන්ම එකක් ලබා ගැනීම
    url_to_process = (req.video_url or req.youtube_url or "").strip()
    if not url_to_process:
        conn.close()
        raise HTTPException(status_code=400, detail="කරුණාකර වලංගු Video URL එකක් ඇතුළත් කරන්න.")

    raw_video = os.path.join(PROJECT_DIR, f"temp_{int(datetime.now().timestamp() * 1000)}.mp4")
    output_data = []

    try:
        video_title, total_duration = download_social_video(url_to_process, raw_video)

        if not is_vip and total_duration > 600:
            raise HTTPException(
                status_code=403,
                detail=f"මෙම වීඩියෝව විනාඩි {math.ceil(total_duration/60)} කි. Free Plan එකේ උපරිම විනාඩි 10 ක වීඩියෝ පමණි."
            )

        PART_DURATION = 180
        total_parts = math.ceil(total_duration / PART_DURATION)
        if total_parts < 1:
            total_parts = 1

        parts_to_generate = total_parts
        if not is_vip:
            remaining_quota = 15 - clips_used
            parts_to_generate = min(total_parts, remaining_quota)

        for i in range(parts_to_generate):
            start_sec = i * PART_DURATION
            current_clip_len = min(PART_DURATION, total_duration - start_sec)
            if current_clip_len <= 3:
                break

            clip_name = f"repurpose_{int(datetime.now().timestamp())}_{i+1}.mp4"
            final_path = os.path.join(OUTPUT_DIR, clip_name)

            meta = generate_metadata_with_gemini(video_title, i + 1, total_parts)
            title_text = (
                meta.get("title", "")
                .replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace(":", "\\:")
                .replace("%", "\\%")
                .replace("[", "\\[")
                .replace("]", "\\]")
                .replace(",", "\\,")
                .replace(";", "\\;")
                .replace("\\n", " ")
            )

            base_filter = (
                "[0:v]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,boxblur=15:5[bg];"
                "[0:v]scale=720:-1,eq=brightness=0.03:saturation=1.12:contrast=1.05,setpts=0.97*PTS[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2[combined];"
                f"[combined]drawtext=text='{title_text[:35]}':fontcolor=yellow:fontsize=36:box=1:boxcolor=black@0.65:boxborderw=10:x=(w-text_w)/2:y=120"
            )

            if not is_vip:
                vf_filter = base_filter + ",drawtext=text='✨ ShortsFlow AI':x=w-tw-25:y=h-th-40:fontsize=22:fontcolor=white@0.55:shadowcolor=black@0.4:shadowx=2:shadowy=2"
            else:
                vf_filter = base_filter

            cmd = [
                FFMPEG_BIN, "-y", "-ss", str(timedelta(seconds=int(start_sec))), "-i", raw_video, 
                "-t", str(int(current_clip_len)),
                "-filter_complex", vf_filter,
                "-af", "atempo=1.03,volume=1.05",
                "-c:v", "libx264", "-crf", "22", "-preset", "veryfast", "-threads", "2",
                "-c:a", "aac", "-b:a", "192k",
                "-map_metadata", "-1",
                final_path
            ]
            subprocess.run(cmd, check=True, timeout=240, capture_output=True, text=True)

            output_data.append({
                "clip_url": f"{PUBLIC_BASE_URL}/output/{clip_name}",
                "title": meta.get("title", ""),
                "description": meta.get("description", ""),
                "hashtags": meta.get("hashtags", "")
            })

        if not is_vip:
            cursor.execute("UPDATE users SET monthly_clips_used = monthly_clips_used + ? WHERE LOWER(TRIM(email)) = ?", (len(output_data), normalize_email(req.email)))
            conn.commit()

        conn.close()
        if os.path.exists(raw_video):
            os.remove(raw_video)

        return {"status": "success", "results": output_data}

    except HTTPException:
        if os.path.exists(raw_video):
            os.remove(raw_video)
        conn.close()
        raise
    except subprocess.CalledProcessError as e:
        if os.path.exists(raw_video):
            os.remove(raw_video)
        conn.close()
        detail = (e.stderr or e.stdout or str(e)).strip()
        raise HTTPException(
            status_code=500,
            detail=f"FFmpeg error: {detail[-400:]}"
        )
    except Exception as e:
        if os.path.exists(raw_video):
            os.remove(raw_video)
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
    )
