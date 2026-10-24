#!/bin/bash
set -e

STORAGE_DIR=/opt/render/project/.render

if [ ! -d "$STORAGE_DIR/ffmpeg" ]; then
  echo "...Downloading FFmpeg"
  mkdir -p "$STORAGE_DIR/ffmpeg"
  cd "$STORAGE_DIR/ffmpeg"
  wget -nc https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
  tar -xf ffmpeg-release-amd64-static.tar.xz --strip-components 1
  rm -f ffmpeg-release-amd64-static.tar.xz
fi

export PATH="$STORAGE_DIR/ffmpeg:$PATH"
pip install -r requirements.txt
