FROM ubuntu:24.04
# ffmpeg 6.1 (нужен для text_align), шрифт с кириллицей
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg fonts-dejavu-core python3 python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir --break-system-packages \
      fastapi==0.115.* "uvicorn[standard]==0.32.*" python-multipart==0.0.* pillow==11.* numpy scipy
WORKDIR /app
COPY app.py render_core.py render_cli.py gh_job.py make_banner.py prepare_overlay.py ./
COPY assets ./assets
ENV OUT_DIR=/data/files ASSETS_DIR=/app/assets
EXPOSE 8000
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
