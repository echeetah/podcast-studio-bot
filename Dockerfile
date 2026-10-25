FROM python:3.12.15-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libarchive-tools tzdata && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY podcast.py bot.py feed.py cuts.py webapp.html /app/
WORKDIR /app
CMD ["python3", "-u", "/app/bot.py"]
