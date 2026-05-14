FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY web_downloader/ ./web_downloader/

RUN mkdir -p downloads

ENV PORT=8080
EXPOSE 8080

CMD ["python", "web_downloader/app.py"]
