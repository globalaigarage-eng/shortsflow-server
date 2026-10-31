import os
import uuid
import json
import subprocess
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="ShortFlow API")

OUTPUT_DIR = "outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=OUTPUT_DIR), name="static")


# --- Data Models ---

class ProcessRequest(BaseModel):
    email: str
    video_url: str
    youtube_url: Optional[str] = ""

class ClipItem(BaseModel):
    clip_url: str
    title: str
    description: str
    hashtags: str

class ProcessResponse(BaseModel):
    status: str
    results: List[ClipItem]

class AuthRequest(BaseModel):
    email: str
    password: str
    device_id: Optional[str] = None

class AuthResponse(BaseModel):
    status: str
    is_vip: bool = True
    is_admin: bool = False
    links_used: int = 0

class ForgotRequest(BaseModel):
    email: str

class ResetRequest(BaseModel):
    email: str
    otp: str
    new_password: str

class VipActivateRequest(BaseModel):
    admin_email: str
    target_user_email: str

class MessageResponse(BaseModel):
    status: str
    message: str


def remove_file(filepath: str):
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
        except Exception:
            pass


# --- Helper Function: Metadata Extract & Viral Formatting ---
def extract_metadata_and_generate_tags(video_url: str):
    """
    yt-dlp හරහා වීඩියෝවේ මුල් තොරතුරු ලබාගෙන
    Viral Title, Description සහ Hashtags සකසයි.
    """
    cmd = [
        "yt-dlp",
        "--dump-single-json",
        "--no-playlist",
        video_url
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        meta = json.loads(result.stdout)
        
        raw_title = meta.get("title", "Viral Live Musical Nonstop")
        uploader = meta.get("uploader", "Artist")
        raw_tags = meta.get("tags", [])
        
        # 1. Viral Click-Worthy Title
        viral_title = f"🔥 {raw_title} | Trending Live Nonstop 💥"
        
        # 2. Rich SEO Description
        viral_description = (
            f"Watch the best moments of {raw_title} by {uploader}!\n"
            f"Enjoy non-stop Sri Lankan live musical party vibes and trending beats. "
            f"Don't forget to like, follow, and share for more nonstop drops! 🎧✨"
        )
        
        # 3. Targeted Viral Hashtags
        base_hashtags = ["#nonstop", "#sinhalamusic", "#reels", "#fyp", "#viral", "#foryou", "#trending", "#liveshow"]
        if raw_tags:
            custom_tags = [f"#{t.replace(' ', '')}" for t in raw_tags[:5] if t]
            base_hashtags.extend(custom_tags)
        
        final_hashtags = " ".join(list(dict.fromkeys(base_hashtags)))
        
        return viral_title, viral_description, final_hashtags

    except Exception:
        # Fallback values
        return (
            "🔥 Trending Sinhala Nonstop Performance | NonStop.lk",
            "Best Sri Lankan live musical nonstop vibes. Enjoy high-energy musical party hits!",
            "#nonstop #sinhalamusic #reels #fyp #viral #foryou #trending #musical"
        )


# --- Endpoints ---

@app.get("/")
def root():
    return {"status": "running", "service": "ShortFlow Backend"}

@app.post("/auth/register", response_model=MessageResponse)
def register(req: AuthRequest):
    return {"status": "success", "message": "User registered successfully"}

@app.post("/auth/login", response_model=AuthResponse)
def login(req: AuthRequest):
    return {"status": "success", "is_vip": True, "is_admin": False, "links_used": 0}

@app.post("/auth/forgot-password", response_model=MessageResponse)
def forgot_password(req: ForgotRequest):
    return {"status": "success", "message": "OTP sent to email"}

@app.post("/auth/reset-password", response_model=MessageResponse)
def reset_password(req: ResetRequest):
    return {"status": "success", "message": "Password reset successfully"}

@app.post("/admin/activate-vip", response_model=MessageResponse)
def activate_vip(req: VipActivateRequest):
    return {"status": "success", "message": f"VIP activated for {req.target_user_email}"}


@app.post("/process-video", response_model=ProcessResponse)
def process_video(req: ProcessRequest, request: Request):
    target_url = req.video_url if req.video_url else req.youtube_url
    if not target_url:
        raise HTTPException(status_code=400, detail="Video URL is required")

    job_id = str(uuid.uuid4())[:8]
    temp_raw = f"temp_{job_id}.mp4"
    output_filename = f"processed_{job_id}.mp4"
    output_path = os.path.join(OUTPUT_DIR, output_filename)

    try:
        # 1. Metadata (Title, Description, Hashtags) Extract කර සැකසීම
        v_title, v_desc, v_tags = extract_metadata_and_generate_tags(target_url)

        # 2. වීඩියෝව බාගත කිරීම
        download_command = [
            "yt-dlp",
            "-f", "best[ext=mp4]/best",
            "-o", temp_raw,
            target_url
        ]
        subprocess.run(download_command, check=True)

        # 3. FFmpeg මඟින් Video & Audio Transform කිරීම
        ffmpeg_command = [
            "ffmpeg", "-y",
            "-i", temp_raw,
            "-vf", "crop=in_w*0.97:in_h*0.97,eq=contrast=1.02:brightness=0.01,setpts=PTS/1.03",
            "-filter:a", "asetrate=44100*1.015,aresample=44100,atempo=1.015,equalizer=f=1000:t=q:w=1:g=1.2",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "24",
            "-c:a", "aac",
            "-b:a", "128k",
            output_path
        ]
        subprocess.run(ffmpeg_command, check=True)

    except subprocess.CalledProcessError as e:
        remove_file(temp_raw)
        remove_file(output_path)
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")
    finally:
        remove_file(temp_raw)

    base_url = str(request.base_url).rstrip("/")
    download_url = f"{base_url}/static/{output_filename}"

    # Title, Description, Hashtags වෙන වෙනම App එකට ලැබෙන ClipItem එක
    clip = ClipItem(
        clip_url=download_url,
        title=v_title,
        description=v_desc,
        hashtags=v_tags
    )

    return ProcessResponse(
        status="success",
        results=[clip]
    )