import os
import re
import sqlite3
import subprocess
import json
import random
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="ShortsFlow Universal AI Backend")

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)
app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")

ADMIN_EMAIL = "anushkamalshan666@gmail.com"
ADMIN_PASSWORD = "@Anushka*malshan2005"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

otp_storage = {}

# --- Database Setup (Clips Tracking & Device Binding) ---
def init_db():
    conn = sqlite3.connect("shortsflow.db")
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

    cursor.execute("SELECT email FROM users WHERE email = ?", (ADMIN_EMAIL,))
    if not cursor.fetchone():
        cursor.execute("INSERT INTO users VALUES (?, ?, 1, '2099-12-31', 0, ?, 'ADMIN_DEVICE')", 
                       (ADMIN_EMAIL, ADMIN_PASSWORD, datetime.now().strftime("%Y-%m")))
    else:
        cursor.execute("UPDATE users SET password = ? WHERE email = ?", (ADMIN_PASSWORD, ADMIN_EMAIL))
    
    conn.commit()
    conn.close()

init_db()

# --- Gemini Metadata Generator ---
def generate_metadata_with_gemini(video_title: str, clip_index: int):
    clean_title = re.sub(r'[^\w\s-]', '', video_title)[:40].strip()
    fallback_title = f"🔥 {clean_title} - Epic Moment"

    if not GEMINI_API_KEY:
        return {
            "title": fallback_title,
            "description": f"Check out this viral moment from {clean_title}! Subscribe for more.",
            "hashtags": "#Shorts #Viral #Trending #FYP #Reels"
        }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = f"""
    Create viral metadata for this video: "{video_title}".
    DO NOT use "Part 1" or "Clip 1". Create a high-CTR, dramatic title.
    Respond ONLY in strict JSON:
    {{
        "title": "Engaging Short Title with emoji (under 50 chars)",
        "description": "Engaging 2-sentence description driving comments",
        "hashtags": "#Shorts #Viral #Trending #FYP #ShortsFlow plus 3 related tags"
    }}
    """
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            raw_text = res_data['candidates'][0]['content']['parts'][0]['text']
            clean_text = raw_text.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_text)
    except Exception:
        return {
            "title": fallback_title,
            "description": f"Unbelievable highlight from {clean_title}! Watch till the end.",
            "hashtags": "#Shorts #Viral #Trending #FYP #ShortsFlow"
        }

# --- Request Models ---
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

# --- API Endpoints ---
@app.post("/auth/register")
def register(req: AuthReq):
    conn = sqlite3.connect("shortsflow.db")
    cursor = conn.cursor()
    current_month = datetime.now().strftime("%Y-%m")
    user_email = req.email.lower().strip()
    
    if user_email != ADMIN_EMAIL.lower():
        if req.device_id and req.device_id != "UNKNOWN":
            cursor.execute("SELECT email FROM users WHERE device_id = ?", (req.device_id,))
            if cursor.fetchone():
                conn.close()
                raise HTTPException(
                    status_code=403, 
                    detail="මෙම දුරකථනයෙන් දැනටමත් Free ගිණුමක් ලියාපදිංචි කර ඇත! එක Phone එකකට එක Free ගිණුමකි."
                )

    try:
        cursor.execute("INSERT INTO users VALUES (?, ?, 0, NULL, 0, ?, ?)", 
                       (user_email, req.password, current_month, req.device_id))
        conn.commit()
        return {"status": "success", "message": "ගිණුම සාදන ලදී!"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="මෙම Email එක දැනටමත් ලියාපදිංචි කර ඇත.")
    finally:
        conn.close()

@app.post("/auth/login")
def login(req: AuthReq):
    conn = sqlite3.connect("shortsflow.db")
    cursor = conn.cursor()
    cursor.execute("SELECT email, is_vip, vip_expiry, monthly_clips_used FROM users WHERE email = ? AND password = ?", 
                   (req.email.lower().strip(), req.password))
    user = cursor.fetchone()
    conn.close()
    
    if not user:
        raise HTTPException(status_code=401, detail="Email හෝ Password වැරදියි.")
    
    email, is_vip, vip_expiry, clips_used = user
    if is_vip and vip_expiry:
        if datetime.now() > datetime.strptime(vip_expiry, "%Y-%m-%d"):
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
    conn = sqlite3.connect("shortsflow.db")
    cursor = conn.cursor()
    cursor.execute("SELECT email FROM users WHERE email = ?", (req.email.lower().strip(),))
    user = cursor.fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=404, detail="මෙම Email ලිපිනය සහිත ගිණුමක් හමු නොවීය.")

    otp = str(random.randint(100000, 999999))
    otp_storage[req.email.lower().strip()] = otp

    return {
        "status": "success", 
        "message": "OTP කේතය ඔබගේ Email එකට යවන ලදී (Demo OTP: " + otp + ")"
    }

@app.post("/auth/reset-password")
def reset_password(req: ResetReq):
    target_email = req.email.lower().strip()
    stored_otp = otp_storage.get(target_email)

    if not stored_otp or stored_otp != req.otp.strip():
        raise HTTPException(status_code=400, detail="OTP අංකය වැරදියි හෝ කල් ඉකුත් වී ඇත.")

    conn = sqlite3.connect("shortsflow.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET password = ? WHERE email = ?", (req.new_password, target_email))
    conn.commit()
    conn.close()

    del otp_storage[target_email]
    return {"status": "success", "message": "Password එක සාර්ථකව වෙනස් කරන ලදී! දැන් Login විය හැක."}

@app.post("/admin/activate-vip")
def activate_vip(req: ActivateVipReq):
    if req.admin_email.lower().strip() != ADMIN_EMAIL.lower():
        raise HTTPException(status_code=403, detail="අවසර නැත.")
    
    expiry = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    conn = sqlite3.connect("shortsflow.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_vip = 1, vip_expiry = ? WHERE email = ?", 
                   (expiry, req.target_user_email.lower().strip()))
    changes = conn.total_changes
    conn.commit()
    conn.close()
    
    if changes == 0:
        raise HTTPException(status_code=404, detail="User හමු නොවීය.")
    return {"status": "success", "message": f"{req.target_user_email} සඳහා VIP දින 30ක් සක්‍රීය විය!"}

# --- YouTube Bot-Block Bypass Downloader Engine ---
def download_media_stream(url: str, output_path: str):
    # 1. YouTube Link එකක්දැයි හඳුනා ගැනීම
    is_youtube = ("youtube.com" in url) or ("youtu.be" in url)

    if is_youtube:
        # YouTube Bot-Block එක bypass කරන Global Proxy Endpoints
        api_gateways = [
            {"endpoint": "https://api.cobalt.tools/api/json", "payload": {"url": url, "vQuality": "720"}},
            {"endpoint": "https://cobalt-api.kwiatekm.tokyo/api/json", "payload": {"url": url, "vQuality": "720"}},
            {"endpoint": "https://co.wuk.sh/api/json", "payload": {"url": url, "vQuality": "720"}}
        ]
        
        for gw in api_gateways:
            try:
                post_data = json.dumps(gw["payload"]).encode('utf-8')
                req = urllib.request.Request(
                    gw["endpoint"],
                    data=post_data,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": "ShortsFlowEngine/2.0"
                    }
                )
                with urllib.request.urlopen(req, timeout=12) as response:
                    res_body = json.loads(response.read().decode('utf-8'))
                    stream_url = res_body.get("url")
                    if stream_url:
                        # Direct MP4 stream එක FFmpeg මඟින් බාගත කිරීම
                        cmd = ["ffmpeg", "-y", "-i", stream_url, "-c", "copy", "-t", "420", output_path]
                        subprocess.run(cmd, check=True)
                        return "Viral YouTube Highlight", 200
            except Exception:
                continue

    # 2. Facebook, TikTok, Instagram, Rumble හෝ YouTube Fallback සඳහා yt-dlp
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'overwrites': True,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36'
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get('title', 'Viral Highlight')
        duration = int(info.get('duration') or 180)
        return title, duration

@app.post("/process-video")
def process_video(req: ProcessVideoReq):
    conn = sqlite3.connect("shortsflow.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_vip, monthly_clips_used FROM users WHERE email = ?", (req.email.lower().strip(),))
    user = cursor.fetchone()
    
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User හමු නොවීය.")
    
    is_vip, clips_used = user[0], user[1] or 0

    max_per_link = 10 if is_vip else 2
    if req.clip_count < 1 or req.clip_count > max_per_link:
        conn.close()
        raise HTTPException(status_code=403, detail=f"ඔබට එකවර සෑදිය හැක්කේ වීඩියෝ 1 සිට {max_per_link} දක්වා ප්‍රමාණයකි.")

    if not is_vip and (clips_used + req.clip_count) > 30:
        conn.close()
        raise HTTPException(status_code=403, detail=f"Free මාසික සීමාව (වීඩියෝ 30) අවසන්! (භාවිත කර ඇත: {clips_used}). Unlimited සඳහා VIP ලබාගන්න.")

    raw_video = f"temp_{int(datetime.now().timestamp())}.mp4"
    output_data = []

    try:
        video_title, total_duration = download_media_stream(req.youtube_url, raw_video)
        
        clip_duration = 32
        usable_duration = max(total_duration - 15, clip_duration * req.clip_count)
        step_interval = usable_duration // (req.clip_count + 1)

        for i in range(req.clip_count):
            start_sec = 8 + (step_interval * (i + 1))
            if start_sec + clip_duration > total_duration:
                start_sec = max(0, total_duration - clip_duration - (i * 2))

            clip_name = f"clip_{int(datetime.now().timestamp())}_{i+1}.mp4"
            final_path = os.path.join(OUTPUT_DIR, clip_name)

            meta = generate_metadata_with_gemini(video_title, i)
            title_text = meta.get("title", "").replace("'", "").replace(":", "")

            # Ultra 1080p Blur Background Filter
            base_filter = (
                "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:10[bg];"
                "[0:v]scale=1080:-1[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2[combined];"
                f"[combined]drawtext=text='{title_text[:36]}':fontcolor=yellow:fontsize=46:box=1:boxcolor=black@0.65:boxborderw=14:x=(w-text_w)/2:y=180"
            )

            if not is_vip:
                vf_filter = base_filter + ",drawtext=text='✨ ShortsFlow AI':x=w-tw-40:y=h-th-60:fontsize=28:fontcolor=white@0.55:shadowcolor=black@0.4:shadowx=2:shadowy=2"
            else:
                vf_filter = base_filter

            # Clear Bitrate Audio/Video Encoding
            cmd = [
                "ffmpeg", "-y", "-ss", str(timedelta(seconds=int(start_sec))), "-i", raw_video, 
                "-t", str(clip_duration),
                "-filter_complex", vf_filter,
                "-c:v", "libx264", "-crf", "22", "-preset", "veryfast",
                "-c:a", "aac", "-b:a", "192k",
                final_path
            ]
            subprocess.run(cmd, check=True)

            output_data.append({
                "clip_url": f"https://shortsflow-server.onrender.com/output/{clip_name}",
                "title": meta.get("title", ""),
                "description": meta.get("description", ""),
                "hashtags": meta.get("hashtags", "")
            })

        if not is_vip:
            cursor.execute("UPDATE users SET monthly_clips_used = monthly_clips_used + ? WHERE email = ?", (req.clip_count, req.email.lower().strip()))
            conn.commit()

        conn.close()
        if os.path.exists(raw_video):
            os.remove(raw_video)

        return {"status": "success", "results": output_data}

    except Exception as e:
        if os.path.exists(raw_video):
            os.remove(raw_video)
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
