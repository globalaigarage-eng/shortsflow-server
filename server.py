import os
import re
import sqlite3
import subprocess
import shutil
import json
import random
import urllib.request
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="ShortsFlow Universal AI Backend")

# Render configuration. Keep this URL aligned with the existing Render service.
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://shortsflow-server.onrender.com"
).strip().rstrip("/")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# IMPORTANT: use the same SQLite path that the original server used.
# All auth, VIP activation, and video-processing endpoints must share this DB.
# Do not switch to a second DB_PATH, otherwise login and /process-video can
# see different user tables and /process-video returns "user record not found".
def get_db_connection():
    conn = sqlite3.connect("shortsflow.db", timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def normalize_email(email: str) -> str:
    return (email or "").strip().casefold()

OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Prefer the FFmpeg binary installed by render-build.sh.
FFMPEG_DIR = "/opt/render/project/.render/ffmpeg"
FFMPEG_BIN = os.path.join(FFMPEG_DIR, "ffmpeg")
if not (os.path.isfile(FFMPEG_BIN) and os.access(FFMPEG_BIN, os.X_OK)):
    FFMPEG_BIN = "ffmpeg"

@app.get("/")
def root():
    return {"service": "ShortsFlow AI Backend", "status": "online", "health": "/health", "docs": "/docs"}

@app.get("/health")
def health():
    if os.path.isabs(FFMPEG_BIN):
        ffmpeg_available = os.path.isfile(FFMPEG_BIN) and os.access(FFMPEG_BIN, os.X_OK)
    else:
        import shutil
        ffmpeg_available = shutil.which(FFMPEG_BIN) is not None
    return {
        "status": "ok",
        "ffmpeg_available": ffmpeg_available,
        "public_base_url_configured": bool(PUBLIC_BASE_URL),
    }

app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

otp_storage = {}

# --- Database Setup ---
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
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN monthly_clips_used INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN device_id TEXT")
    except sqlite3.OperationalError:
        pass

    # Admin credentials must be configured as Render environment variables.
    # Do not create an admin account with an empty/default password.
    if ADMIN_EMAIL and ADMIN_PASSWORD:
        cursor.execute("SELECT email FROM users WHERE email = ?", (ADMIN_EMAIL,))
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO users VALUES (?, ?, 1, '2099-12-31', 0, ?, 'ADMIN_DEVICE')",
                (ADMIN_EMAIL, ADMIN_PASSWORD, datetime.now().strftime("%Y-%m"))
            )
        else:
            cursor.execute(
                "UPDATE users SET password = ? WHERE email = ?",
                (ADMIN_PASSWORD, ADMIN_EMAIL)
            )
    
    conn.commit()
    conn.close()

init_db()

# --- Gemini Smart Title & Hooks ---
def generate_metadata_with_gemini(video_title: str, clip_index: int):
    clean_title = re.sub(r'[^\w\s-]', '', video_title)[:40].strip()
    fallback_title = f"ðŸ”¥ {clean_title} (Must Watch)"

    if not GEMINI_API_KEY:
        return {
            "title": fallback_title,
            "description": f"Highlight moment from {clean_title}! Share and follow.",
            "hashtags": "#Shorts #Viral #Trending #FYP #Reels"
        }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = f"""
    Create a viral Title (under 45 chars) with an emoji for: "{video_title}".
    Do NOT include Part 1 or Clip 1.
    Respond ONLY in strict JSON:
    {{
        "title": "Dramatic Title",
        "description": "Engaging 2-sentence description",
        "hashtags": "#Shorts #Viral #Trending #FYP"
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
            "hashtags": "#Shorts #Viral #Trending #FYP"
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

class ProcessVideoReq(BaseModel):
    email: str
    youtube_url: str
    clip_count: int

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
            raise HTTPException(status_code=403, detail="à¶¸à·™à¶¸ à¶¯à·”à¶»à¶šà¶®à¶±à¶ºà·™à¶±à·Š à¶¯à·à¶±à¶§à¶¸à¶­à·Š Free à¶œà·’à¶«à·”à¶¸à¶šà·Š à¶½à·’à¶ºà·à¶´à¶¯à·’à¶‚à¶ à·’ à¶šà¶» à¶‡à¶­!")

    try:
        cursor.execute("INSERT INTO users VALUES (?, ?, 0, NULL, 0, ?, ?)", 
                       (user_email, req.password, current_month, req.device_id))
        conn.commit()
        return {"status": "success", "message": "à¶œà·’à¶«à·”à¶¸ à·ƒà·à¶¯à¶± à¶½à¶¯à·“!"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="à¶¸à·™à¶¸ Email à¶‘à¶š à¶¯à·à¶±à¶§à¶¸à¶­à·Š à¶½à·’à¶ºà·à¶´à¶¯à·’à¶‚à¶ à·’ à¶šà¶» à¶‡à¶­.")
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
        raise HTTPException(status_code=401, detail="Email à·„à· Password à·€à·à¶»à¶¯à·’à¶ºà·’.")
    
    email, is_vip, vip_expiry, clips_used = user
    if is_vip and vip_expiry and datetime.now() > datetime.strptime(vip_expiry, "%Y-%m-%d"):
        is_vip = 0
            
    return {
        "status": "success",
        "email": email,
        "is_vip": bool(is_vip),
        "is_admin": (email.lower() == ADMIN_EMAIL.lower()),
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
        raise HTTPException(status_code=404, detail="Email à¶½à·’à¶´à·’à¶±à¶º à·„à¶¸à·” à¶±à·œà·€à·“à¶º.")

    otp = str(random.randint(100000, 999999))
    otp_storage[normalize_email(req.email)] = otp
    return {"status": "success", "message": "OTP à¶šà·šà¶­à¶º Email à·€à·™à¶­ à¶ºà·€à¶± à¶½à¶¯à·“ (Demo: " + otp + ")"}

@app.post("/auth/reset-password")
def reset_password(req: ResetReq):
    target_email = normalize_email(req.email)
    stored_otp = otp_storage.get(target_email)

    if not stored_otp or stored_otp != req.otp.strip():
        raise HTTPException(status_code=400, detail="OTP à¶…à¶‚à¶šà¶º à·€à·à¶»à¶¯à·’à¶ºà·’.")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET password = ? WHERE email = ?", (req.new_password, target_email))
    conn.commit()
    conn.close()

    del otp_storage[target_email]
    return {"status": "success", "message": "Password à¶ºà·à·€à¶­à·Šà¶šà·à¶½à·“à¶± à·€à·’à¶º!"}

@app.post("/admin/activate-vip")
def activate_vip(req: ActivateVipReq):
    if not ADMIN_EMAIL or not ADMIN_PASSWORD or normalize_email(req.admin_email) != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="à¶…à·€à·ƒà¶» à¶±à·à¶­.")
    
    expiry = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_vip = 1, vip_expiry = ? WHERE email = ?", 
                   (expiry, normalize_email(req.target_user_email)))
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"{req.target_user_email} VIP à·ƒà¶šà·Šâ€à¶»à·“à¶º à·€à·’à¶º!"}

# --- Video ID Extractor ---
def extract_youtube_id(url: str):
    patterns = [
        r'(?:v=|\/|youtu\.be\/|shorts\/)([0-9A-Za-z_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

# --- YouTube & Video Downloader Engine ---
def download_media_stream(url: str, output_path: str):
    """
    Download media with yt-dlp.
    Optional YouTube cookies:
      - Set YOUTUBE_COOKIES_FILE in Render Environment to the absolute path
        of a cookies.txt file that exists on the deployed service, OR
      - Place cookies.txt beside server.py (not recommended for public repos).
    """
    clean_url = url.strip()
    video_id = extract_youtube_id(clean_url)

    # yt-dlp needs a real, readable Netscape-format cookies file to use cookies.
    cookie_path = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()
    if not cookie_path:
        # Supports either common filename. The file must still be valid
        # Netscape-format cookies exported from your own browser session.
        for cookie_name in ("cookies.txt", "cookies.file"):
            local_cookie_path = os.path.join(PROJECT_DIR, cookie_name)
            if os.path.isfile(local_cookie_path):
                cookie_path = local_cookie_path
                break

    ydl_opts = {
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
        "outtmpl": output_path,
        "overwrites": True,
        "quiet": False,
        "no_warnings": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        },
    }

    if cookie_path:
        if not os.path.isfile(cookie_path):
            raise RuntimeError(
                "YOUTUBE_COOKIES_FILE is set, but the file does not exist on the server."
            )
        if not os.access(cookie_path, os.R_OK):
            raise RuntimeError(
                "The YouTube cookies file exists but is not readable by the server."
            )
        ydl_opts["cookiefile"] = cookie_path

    target_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else clean_url

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=True)
            title = info.get("title", "Viral Clip")
            duration = int(info.get("duration") or 180)
            return title, duration
    except Exception as exc:
        message = str(exc)
        if "Sign in to confirm you're not a bot" in message or "confirm you're not a bot" in message:
            if not cookie_path:
                raise RuntimeError(
                    "YouTube requires authentication for this request. The server did not load "
                    "a cookies file. Set YOUTUBE_COOKIES_FILE to a valid server-side "
                    "Netscape-format cookies.txt path, then redeploy."
                ) from exc
            raise RuntimeError(
                "YouTube still rejected the request even with the configured cookies. "
                "The cookies may be expired/invalid, or the hosting IP may be blocked. "
                "Export a fresh cookies file from your own signed-in browser session and "
                "store it privately; do not commit it to a public repository."
            ) from exc
        raise RuntimeError(f"yt-dlp download failed: {message}") from exc

@app.post("/process-video")
def process_video(req: ProcessVideoReq):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_vip, monthly_clips_used FROM users WHERE LOWER(TRIM(email)) = ?", (normalize_email(req.email),))
    user = cursor.fetchone()
    
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="à¶¸à·™à¶¸ email à¶‘à¶šà¶§ user record à¶‘à¶š database à¶‘à¶šà·š à¶±à·à¶­. Login à·ƒà·„ video request à¶‘à¶š à¶‘à¶šà¶¸ email/database à¶‘à¶š à¶·à·à·€à·’à¶­ à¶šà¶»à¶±à·€à·à¶¯ à¶¶à¶½à¶±à·Šà¶±.")
    
    is_vip, clips_used = user[0], user[1] or 0

    max_per_link = 10 if is_vip else 2
    if req.clip_count < 1 or req.clip_count > max_per_link:
        conn.close()
        raise HTTPException(status_code=403, detail=f"à·€à·“à¶©à·’à¶ºà· 1 à·ƒà·’à¶§ {max_per_link} à¶¯à¶šà·Šà·€à· à¶­à·à¶»à¶±à·Šà¶±.")

    if not is_vip and (clips_used + req.clip_count) > 30:
        conn.close()
        raise HTTPException(status_code=403, detail=f"Free à¶¸à·à·ƒà·’à¶š à·ƒà·“à¶¸à·à·€ (à·€à·“à¶©à·’à¶ºà· 30) à¶…à·€à·ƒà¶±à·Š! (à¶¯à·à¶±à¶§: {clips_used}). VIP à¶½à¶¶à·à¶œà¶±à·Šà¶±.")

    raw_video = os.path.join(PROJECT_DIR, f"temp_{int(datetime.now().timestamp() * 1000)}.mp4")
    output_data = []

    try:
        video_title, total_duration = download_media_stream(req.youtube_url, raw_video)
        
        clip_duration = 30
        usable_duration = max(total_duration - 15, clip_duration * req.clip_count)
        step_interval = usable_duration // (req.clip_count + 1)

        for i in range(req.clip_count):
            start_sec = 8 + (step_interval * (i + 1))
            if start_sec + clip_duration > total_duration:
                start_sec = max(0, total_duration - clip_duration - (i * 2))

            clip_name = f"clip_{int(datetime.now().timestamp())}_{i+1}.mp4"
            final_path = os.path.join(OUTPUT_DIR, clip_name)

            meta = generate_metadata_with_gemini(video_title, i)
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
                "[0:v]scale=720:-1[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2[combined];"
                f"[combined]drawtext=text='{title_text[:35]}':fontcolor=yellow:fontsize=36:box=1:boxcolor=black@0.65:boxborderw=10:x=(w-text_w)/2:y=120"
            )

            if not is_vip:
                vf_filter = base_filter + ",drawtext=text='âœ¨ ShortsFlow AI':x=w-tw-25:y=h-th-40:fontsize=22:fontcolor=white@0.55:shadowcolor=black@0.4:shadowx=2:shadowy=2"
            else:
                vf_filter = base_filter

            cmd = [
                FFMPEG_BIN, "-y", "-ss", str(timedelta(seconds=int(start_sec))), "-i", raw_video, 
                "-t", str(clip_duration),
                "-filter_complex", vf_filter,
                "-c:v", "libx264", "-crf", "23", "-preset", "veryfast", "-threads", "2",
                "-c:a", "aac", "-b:a", "128k",
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
            cursor.execute("UPDATE users SET monthly_clips_used = monthly_clips_used + ? WHERE email = ?", (req.clip_count, normalize_email(req.email)))
            conn.commit()

        conn.close()
        if os.path.exists(raw_video):
            os.remove(raw_video)

        return {"status": "success", "results": output_data}

    except subprocess.CalledProcessError as e:
        if os.path.exists(raw_video):
            os.remove(raw_video)
        conn.close()
        detail = (e.stderr or e.stdout or str(e)).strip()
        raise HTTPException(
            status_code=500,
            detail=f"FFmpeg processing failed: {detail[-1200:]}"
        )
    except subprocess.TimeoutExpired:
        if os.path.exists(raw_video):
            os.remove(raw_video)
        conn.close()
        raise HTTPException(
            status_code=504,
            detail="FFmpeg processing timed out. Try a shorter source video or fewer clips."
        )
    except Exception as e:
        if os.path.exists(raw_video):
            os.remove(raw_video)
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
