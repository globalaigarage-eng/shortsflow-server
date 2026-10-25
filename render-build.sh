
#!/usr/bin/env bash
set -euo pipefail

STORAGE_DIR="/opt/render/project/.render"
FFMPEG_DIR="$STORAGE_DIR/ffmpeg"

mkdir -p "$FFMPEG_DIR"

if [ ! -x "$FFMPEG_DIR/ffmpeg" ]; then
    echo "Downloading FFmpeg..."

    cd "$FFMPEG_DIR"

    wget -O ffmpeg-release-amd64-static.tar.xz \
      https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz

    tar -xf ffmpeg-release-amd64-static.tar.xz \
      --strip-components=1

    rm -f ffmpeg-release-amd64-static.tar.xz
fi

chmod +x "$FFMPEG_DIR/ffmpeg"

if [ -f "$FFMPEG_DIR/ffprobe" ]; then
    chmod +x "$FFMPEG_DIR/ffprobe"
fi

export PATH="$FFMPEG_DIR:$PATH"

echo "Checking FFmpeg..."
"$FFMPEG_DIR/ffmpeg" -version

echo "Installing Python requirements..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "Build completed successfully."
