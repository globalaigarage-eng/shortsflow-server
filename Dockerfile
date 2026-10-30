FROM python:3.11-slim

# FFmpeg සහ Font libraries ස්ථාපනය
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-liberation \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# දත්ත ෆෝල්ඩර සාදා අවසර ලබා දීම
RUN mkdir -p /app/data/output /app/data/tmp

EXPOSE 8000

CMD ["python", "server.py"]
