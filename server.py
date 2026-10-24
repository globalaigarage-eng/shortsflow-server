import os
import sqlite3
import subprocess
import json
import urllib.request
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="ShortsFlow AI Backend")

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)
app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")

ADMIN_EMAIL = "anushkamalshan666@gmail.com"

# GitHub Block නොවීමට API Key එක Environment Variable එකෙන් කියවයි
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# --- Database Setup ---
def init_db():
    conn = sqlite3.connect("shortsflow.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            password TEXT,
            is_vip INTEGER DEFAULT 0,
            vip_expiry TEXT,
            monthly_links_used INTEGER DEFAULT 0,
            last_reset_month TEXT
        )
    """)
    cursor.execute("SELECT email FROM users WHERE email = ?", (ADMIN_EMAIL,))
    if not cursor.fetchone():
        cursor.execute("INSERT INTO users VALUES (?, ?, 1, '2099-12-31', 0, ?)", 
                       (ADMIN_EMAIL, "admin123", datetime.now().strftime("%Y-%m")))
    conn.commit()
    conn.close()

init_db()

# --- Gemini AI Metadata Generator ---
def generate_metadata_with_gemini(video_title: str, clip_index: int):
    # API Key එකක් නොමැති නම් default template එකක් ලබා දෙයි
    if not GEMINI_API_KEY:
        return {
            "title": f"🔥 Viral Highlight Part {clip_index + 1} - {video_title[:30]}",
            "description": "Watch this amazing short clip! Don't forget to like, share and subscribe for more viral content.",
            "hashtags": "#Shorts #Viral #Trending #FYP #ShortsFlow"
        }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = f"""
    You are an expert viral content creator.
    Based on the YouTube video title: "{video_title}", create high-performing metadata for Clip #{clip_index + 1}.
    Respond ONLY in strict JSON format with exactly these three keys:
    {{
        "title": "A high-CTR, curiosity-inducing Short Title (under 60 chars) with emojis",
        "description": "Short engaging 2-sentence description encouraging likes and subscribes",
        "hashtags": "#Shorts #Viral #Trending #FYP #YouTubeShorts plus 3 niche specific tags"
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
            "description": "Check out this amazing short clip! Like, comment and subscribe for more amazing content daily.",
            "hashtags": "#Shorts #Viral #Trending #FYP #ShortsFlow"
        }

# --- Request Models ---
class AuthReq(BaseModel):
    email: str
    password: str

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
    try:
        cursor.execute("INSERT INTO users VALUES (?, ?, 0, NULL, 0, ?)", 
                       (req.email.lower().strip(), req.password, current_month))
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
    cursor.execute("SELECT email, is_vip, vip_expiry, monthly_links_used FROM users WHERE email = ? AND password = ?", 
                   (req.email.lower().strip(), req.password))
    user = cursor.fetchone()
    conn.close()
    
    if not user:
        raise HTTPException(status_code=401, detail="Email හෝ Password වැරදියි.")
    
    email, is_vip, vip_expiry, links_used = user
    if is_vip and vip_expiry:
        if datetime.now() > datetime.strptime(vip_expiry, "%Y-%m-%d"):
            is_vip = 0
            
    return {
        "status": "success",
        "email": email,
        "is_vip": bool(is_vip),
        "is_admin": (email.lower() == ADMIN_EMAIL.lower()),
        "links_used": links_used
    }

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

@app.post("/process-video")
def process_video(req: ProcessVideoReq):
    conn = sqlite3.connect("shortsflow.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_vip, monthly_links_used FROM users WHERE email = ?", (req.email.lower().strip(),))
    user = cursor.fetchone()
    
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User හමු නොවීය.")
    
    is_vip, links_used = user[0], user[1]

    # Limits: Free = උපරිම 2 | VIP = උපරිම 10
    max_allowed = 10 if is_vip else 2
    if req.clip_count > max_allowed:
        conn.close()
        raise HTTPException(status_code=403, detail=f"ඔබට තෝරාගත හැක්කේ උපරිම clips {max_allowed}කි.")

    if not is_vip and links_used >= 15:
        conn.close()
        raise HTTPException(status_code=403, detail="Free මාසික සීමාව (Links 15) අවසන්. Unlimited සඳහා VIP ලබාගන්න!")

    raw_video = f"temp_{int(datetime.now().timestamp())}.mp4"
    output_data = []
    
    try:
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'outtmpl': raw_video,
            'overwrites': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.youtube_url, download=True)
            video_title = info.get('title', 'Viral Highlight')

        for i in range(req.clip_count):
            start_sec = 20 + (i * 35)
            clip_name = f"clip_{int(datetime.now().timestamp())}_{i+1}.mp4"
            final_path = os.path.join(OUTPUT_DIR, clip_name)

            if not is_vip:
                vf_filter = "crop=ih*(9/16):ih:(iw-ow)/2:0,drawtext=text='ShortsFlow Free':x=w-tw-20:y=h-th-40:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.5"
            else:
                vf_filter = "crop=ih*(9/16):ih:(iw-ow)/2:0"

            cmd = [
                "ffmpeg", "-y", "-ss", str(timedelta(seconds=start_sec)), "-i", raw_video, "-t", "30",
                "-vf", vf_filter, "-c:v", "libx264", "-c:a", "aac", "-preset", "fast", final_path
            ]
            subprocess.run(cmd, check=True)

            meta = generate_metadata_with_gemini(video_title, i)
            output_data.append({
                "clip_url": f"https://shortsflow-api.onrender.com/output/{clip_name}",
                "title": meta.get("title", ""),
                "description": meta.get("description", ""),
                "hashtags": meta.get("hashtags", "")
            })

        if not is_vip:
            cursor.execute("UPDATE users SET monthly_links_used = monthly_links_used + 1 WHERE email = ?", (req.email.lower().strip(),))
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
