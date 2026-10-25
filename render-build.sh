#!/usr/bin/env bash
set -euo pipefail

STORAGE_DIR="/opt/render/project/.render"
FFMPEG_DIR="$STORAGE_DIR/ffmpeg"
DENO_DIR="$STORAGE_DIR/deno"
mkdir -p "$FFMPEG_DIR" "$DENO_DIR"

if [ ! -x "$FFMPEG_DIR/ffmpeg" ]; then
    echo "Downloading FFmpeg..."
    cd "$FFMPEG_DIR"
    wget -O ffmpeg-release-amd64-static.tar.xz \
      https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
    tar -xf ffmpeg-release-amd64-static.tar.xz --strip-components=1
    rm -f ffmpeg-release-amd64-static.tar.xz
fi

chmod +x "$FFMPEG_DIR/ffmpeg"
if [ -f "$FFMPEG_DIR/ffprobe" ]; then
    chmod +x "$FFMPEG_DIR/ffprobe"
fi

echo "Checking FFmpeg..."
"$FFMPEG_DIR/ffmpeg" -version

echo "Installing Deno JavaScript runtime for yt-dlp YouTube extraction..."
if [ ! -x "$DENO_DIR/bin/deno" ]; then
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL="$DENO_DIR" sh
fi
"$DENO_DIR/bin/deno" --version

echo "Installing Python requirements and current yt-dlp..."
python -m pip install --upgrade pip
python -m pip install --upgrade -r requirements.txt
python -m pip install --upgrade "yt-dlp[default]>=2026.10.21"
python -m yt_dlp --version

echo "Build completed successfully."
