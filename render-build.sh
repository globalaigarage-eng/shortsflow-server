#!/usr/bin/env bash
# exit on error
set -o errexit

pip install -r requirements.txt

# Render පරිසරයට Static FFmpeg ලබාගැනීම
FFMPEG_DIR="$HOME/.render/ffmpeg"
if [ ! -f "$FFMPEG_DIR/ffmpeg" ]; then
  echo "Downloading static FFmpeg..."
  mkdir -p "$FFMPEG_DIR"
  curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz | tar xJ --strip-components=1 -C "$FFMPEG_DIR"
fi

export PATH="$FFMPEG_DIR:$PATH"
echo "FFmpeg installed successfully!"
