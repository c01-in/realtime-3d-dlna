FROM python:3.12-slim-trixie AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --index-url https://download.pytorch.org/whl/cu128 torch==2.8.0 \
 && python -m pip install -r requirements.txt
FROM runtime AS app
COPY serve.py assets.py ovda.py video.py stream.py library.py index.html /app/
CMD ["python", "serve.py", "--data-dir", "/data", "--media", "/media"]
