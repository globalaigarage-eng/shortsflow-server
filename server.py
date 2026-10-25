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

# Keep SQLite in one stable location. On Render, attach a persistent disk
# mounted at /var/data and set SHORTSFLOW_DB_PATH=/var/data/shortsflow.db;
# otherwise the free ephemeral filesystem can lose user/VIP records on redeploy.
DB_PATH = os.environ.get(
    "SHORTSFLOW_DB_PATH", os.path.join(PROJECT_DIR, "shortsflow.db")
).strip()
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.abspath(os.path.join(PROJECT_DIR, DB_PATH))

# One-time compatibility: if an older deployment created the DB in its
# working directory, copy it to the stable path rather than silently losing users.
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
        cursor.execute("SELECT email FROM users WHERE LOWER(TRIM(email)) = ?", (normalize_email(ADMIN_EMAIL),))
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO users (email, password, is_vip, vip_expiry, monthly_clips_used, last_reset_month, device_id) "
                "VALUES (?, ?, 1, '2099-12-31', 0, ?, 'ADMIN_DEVICE')",
                (normalize_email(ADMIN_EMAIL), ADMIN_PASSWORD, datetime.now().strftime("%Y-%m"))
            )
        else:
            cursor.execute(
                "UPDATE users SET password = ?, is_vip = 1, vip_expiry = '2099-12-31' "
                "WHERE LOWER(TRIM(email)) = ?",
                (ADMIN_PASSWORD, normalize_email(ADMIN_EMAIL))
            )
    
    conn.commit()
    conn.close()

init_db()

# --- Gemini Smart Title & Hooks ---
def generate_metadata_with_gemini(video_title: str, clip_index: int):
    clean_title = re.sub(r'[^\w\s-]', '', video_title)[:40].strip()
    fallback_title = f"🔥 {clean_title} (Must Watch)"

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
    """Download one public video with yt-dlp and report actionable errors.

    Optional cookies must be a valid, private Netscape-format cookies.txt file.
    Set YOUTUBE_COOKIES_FILE to its absolute path in Render Environment.
    Never commit cookies or share them publicly. Cookies cannot remove a hosting-IP
    rate limit, and this function deliberately does not try to bypass YouTube blocks.
    """
    clean_url = (url or "").strip()
    if not clean_url:
        raise HTTPException(status_code=400, detail="කරුණාකර video URL එකක් ඇතුළත් කරන්න.")

    video_id = extract_youtube_id(clean_url)
    target_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else clean_url

    # Only use cookies when the operator explicitly configures a private,
    # fresh cookies.txt file. Do not auto-load stale cookies from the repository.
    cookie_path = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()

    ydl_opts = {
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
        "outtmpl": output_path,
        "overwrites": True,
        "quiet": True,
        "no_warnings": False,
        "socket_timeout": 30,
        # Do not hammer YouTube after a rate limit. A 429 should be allowed
        # to cool down rather than triggering a retry storm.
        "retries": 0,
        "fragment_retries": 0,
        "extractor_retries": 0,
        "sleep_interval_requests": 2,
        "noplaylist": True,
    }

    # yt-dlp's current YouTube extractor can use Deno for JavaScript challenge
    # solving. render-build.sh installs Deno at this stable path. If absent,
    # yt-dlp still runs but may be unable to extract some YouTube videos.
    deno_path = os.environ.get(
        "DENO_BIN", "/opt/render/project/.render/deno/bin/deno"
    ).strip()
    if deno_path and os.path.isfile(deno_path) and os.access(deno_path, os.X_OK):
        ydl_opts["js_runtimes"] = {"deno": {"path": deno_path}}

    if cookie_path:
        if not os.path.isfile(cookie_path):
            raise HTTPException(
                status_code=503,
                detail="YOUTUBE_COOKIES_FILE path එක server එකේ හමු නොවීය. Render Environment path එක පරීක්ෂා කරන්න."
            )
        if not os.access(cookie_path, os.R_OK):
            raise HTTPException(
                status_code=503,
                detail="YouTube cookies file එක කියවිය නොහැක. File permissions පරීක්ෂා කරන්න."
            )
        ydl_opts["cookiefile"] = cookie_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=True)
            if not info:
                raise RuntimeError("yt-dlp returned no video information")
            title = info.get("title") or "Viral Clip"
            duration = int(info.get("duration") or 180)
            return title, duration
    except HTTPException:
        raise
    except Exception as exc:
        message = str(exc)
        lowered = message.lower()

        if "429" in lowered or "too many requests" in lowered:
            raise HTTPException(
                status_code=429,
                detail=(
                    "YouTube විසින් ShortsFlow server එකේ requests තාවකාලිකව සීමා කර ඇත (HTTP 429). "
                    "මෙය app email එකේ හෝ Public URL එකේ දෝෂයක් නොවේ. නැවත නැවත උත්සාහ නොකර ටික වේලාවකට පසු නැවත උත්සාහ කරන්න. "
                    "Render hosting IP එක දිගටම rate-limit වුවහොත් YouTube download සඳහා වෙනත් නිල/අවසර ලත් source එකක් අවශ්‍ය වේ."
                )
            ) from exc

        if "sign in to confirm you're not a bot" in lowered or "confirm you're not a bot" in lowered:
            if not cookie_path:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "YouTube bot verification ඉල්ලයි. Server එකේ valid cookies file එකක් සකසා නැත. "
                        "Cookies අවශ්‍ය නම් ඔබට අයිති signed-in session එකෙන් export කළ, වලංගු Netscape cookies file එකක් "
                        "private storage එකක තබා YOUTUBE_COOKIES_FILE සකසන්න. Cookies public repository එකකට දාන්න එපා."
                    )
                ) from exc
            raise HTTPException(
                status_code=503,
                detail=(
                    "YouTube bot verification ප්‍රතික්ෂේප කළා. Cookies expired/invalid විය හැකියි, නැත්නම් Render IP එක සීමා කර තිබිය හැකියි. "
                    "Fresh cookies භාවිත කරලාත් HTTP 429 හෝ bot check එක පවතිනවා නම් එය code එකෙන් පමණක් විසඳිය නොහැක; YouTube සීමාව ඉවත් වනතුරු ඉන්න හෝ අවසර ලත් වෙනත් video source එකක් භාවිත කරන්න."
                )
            ) from exc

        if "cookie" in lowered and ("invalid" in lowered or "expired" in lowered or "no longer valid" in lowered):
            raise HTTPException(
                status_code=503,
                detail="YOUTUBE_COOKIES_FILE හි cookies වලංගු නැත හෝ කල් ඉකුත් වී ඇත. ඒවා private storage එකේ fresh Netscape cookies file එකකින් ප්‍රතිස්ථාපනය කරන්න, නැත්නම් YOUTUBE_COOKIES_FILE ඉවත් කර public video එකක් පරීක්ෂා කරන්න. Cookies GitHub වෙත upload නොකරන්න."
            ) from exc

        raise HTTPException(
            status_code=502,
            detail=f"YouTube වෙතින් video ලබාගැනීමට නොහැකි විය: {message[:500]}"
        ) from exc

@app.post("/process-video")
def process_video(req: ProcessVideoReq):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_vip, monthly_clips_used FROM users WHERE LOWER(TRIM(email)) = ?", (normalize_email(req.email),))
    user = cursor.fetchone()
    
    if not user:
        # Some app builds keep the signed-in session locally and may reach this
        # endpoint before the backend has a matching users row. Create a FREE
        # record so video processing does not stop with the old 404. Never grant
        # VIP to an arbitrary email; only the configured admin gets admin VIP.
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
            cursor.execute(
                "SELECT is_vip, monthly_clips_used FROM users WHERE LOWER(TRIM(email)) = ?",
                (email_key,),
            )
            user = cursor.fetchone()
        except sqlite3.Error as exc:
            conn.close()
            raise HTTPException(status_code=500, detail=f"User record එක සකස් කිරීමට නොහැකි විය: {exc}")
        if not user:
            conn.close()
            raise HTTPException(status_code=500, detail="User record එක සකස් කිරීමට නොහැකි විය.")

    is_vip, clips_used = user[0], user[1] or 0

    max_per_link = 10 if is_vip else 2
    if req.clip_count < 1 or req.clip_count > max_per_link:
        conn.close()
        raise HTTPException(status_code=403, detail=f"වීඩියෝ 1 සිට {max_per_link} දක්වා තෝරන්න.")

    if not is_vip and (clips_used + req.clip_count) > 30:
        conn.close()
        raise HTTPException(status_code=403, detail=f"Free මාසික සීමාව (වීඩියෝ 30) අවසන්! (දැනට: {clips_used}). VIP ලබාගන්න.")

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
                vf_filter = base_filter + ",drawtext=text='✨ ShortsFlow AI':x=w-tw-25:y=h-th-40:fontsize=22:fontcolor=white@0.55:shadowcolor=black@0.4:shadowx=2:shadowy=2"
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
            cursor.execute("UPDATE users SET monthly_clips_used = monthly_clips_used + ? WHERE LOWER(TRIM(email)) = ?", (req.clip_count, normalize_email(req.email)))
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

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
    )
