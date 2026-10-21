FROM python:3.12.1-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# ffmpeg = trim / merge / voice-tag ke liye zaroori
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python3", "bot.py"]
