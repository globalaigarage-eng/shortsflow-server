FROM python:3.10-slim

# FFmpeg සහ අවශ්‍ය මෙවලම් ස්ථාපනය
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Packages install කිරීම
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source code එක container එකට copy කිරීම
COPY . .

# Static file output folder එක සෑදීම
RUN mkdir -p outputs

EXPOSE 8000

# Server එක start කිරීම
CMD ["uvicorn", "main.py:app", "--host", "0.0.0.0", "--port", "8000"]