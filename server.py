import os
import sqlite3
import subprocess
import json
import random
import urllib.request
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

# --- Database Setup (Monthly Clips Count Tracking) ---
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
    # Database migration
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
    if not GEMINI_API_KEY:
        return {
            "title": f"🔥 Viral Highlight Part {clip_index + 1} - {video_title[:30]}",
            "description": "Watch this amazing short clip! Like, comment and subscribe for more viral content.",
            "hashtags": "#Shorts #Viral #Trending #FYP #ShortsFlow"
        }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = f"""
    You are an expert viral content creator.
    Based on the video title: "{video_title}", create high-performing metadata for Clip #{clip_index + 1}.
    Respond ONLY in strict JSON format with exactly these three keys:
    {{
        "title": "A high-CTR, curiosity-inducing Short Title (under 60 chars) with emojis",
        "description": "Short engaging 2-sentence description encouraging likes and subscribes",
        "hashtags": "#Shorts #Viral #Trending #FYP #Reels #TikTok plus 3 niche tags"
    }}
    """
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
        with urllib.request.urlopen(req, timeout=12) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            raw_text = res_data['candidates'][0]['content']['parts'][0]['text']
            clean_text = raw_text.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_text)
    except Exception:
        return {
            "title": f"🔥 Must Watch Highlight - Part {clip_index + 1}",
            "description": "Check out this amazing short clip! Like, comment and subscribe.",
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
            existing = cursor.fetchone()
            if existing:
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

# Universal Downloader Engine
def universal_download(url: str, output_path: str):
    # YouTube, Facebook, TikTok, Instagram, Rumble සියල්ලටම සාර්ථකව ක්‍රියාකරන Engine එක
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'overwrites': True,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['tv_embedded', 'android', 'mweb']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
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

    # Free අයට එක Link එකකින් උපරිම clips 2යි | VIP අයට උපරිම 10යි
    max_per_link = 10 if is_vip else 2
    if req.clip_count < 1 or req.clip_count > max_per_link:
        conn.close()
        raise HTTPException(status_code=403, detail=f"ඔබට එකවර සෑදිය හැක්කේ වීඩියෝ 1 සිට {max_per_link} දක්වා ප්‍රමාණයකි.")

    # Free අයට මාසයට උපරිම වීඩියෝ 30යි (Clips 30 limit)
    if not is_vip and (clips_used + req.clip_count) > 30:
        conn.close()
        raise HTTPException(status_code=403, detail=f"Free මාසික සීමාව (වීඩියෝ 30) අවසන්! (දැනට භාවිතා කර ඇත: {clips_used}). Unlimited සඳහා VIP ලබාගන්න.")

    raw_video = f"temp_{int(datetime.now().timestamp())}.mp4"
    output_data = []

    try:
        video_title, total_duration = universal_download(req.youtube_url, raw_video)
        
        clip_duration = 30
        usable_duration = max(total_duration - 15, clip_duration * req.clip_count)
        step_interval = usable_duration // (req.clip_count + 1)

        for i in range(req.clip_count):
            start_sec = 8 + (step_interval * (i + 1))
            if start_sec + clip_duration > total_duration:
                start_sec = max(0, total_duration - clip_duration - (i * 2))

            clip_name = f"clip_{int(datetime.now().timestamp())}_{i+1}.mp4"
            final_path = os.path.join(OUTPUT_DIR, clip_name)

            if not is_vip:
                # ලා සුදු Semi-transparent Watermark එක
                vf_filter = "crop=ih*(9/16):ih:(iw-ow)/2:0,drawtext=text='✨ ShortsFlow AI':x=w-tw-35:y=h-th-50:fontsize=24:fontcolor=white@0.55:shadowcolor=black@0.4:shadowx=2:shadowy=2"
            else:
                vf_filter = "crop=ih*(9/16):ih:(iw-ow)/2:0"

            cmd = [
                "ffmpeg", "-y", "-ss", str(timedelta(seconds=int(start_sec))), "-i", raw_video, 
                "-t", str(clip_duration),
                "-vf", vf_filter, "-c:v", "libx264", "-c:a", "aac", "-preset", "ultrafast", final_path
            ]
            subprocess.run(cmd, check=True)

            meta = generate_metadata_with_gemini(video_title, i)
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
