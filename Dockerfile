FROM python:3.11-slim

# System deps: libgomp1 for PyTorch OpenMP, libsndfile1 for soundfile, ffmpeg for MP3/M4A
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgomp1 libsndfile1 ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install PyTorch CPU-only (before other deps to cache this large layer)
RUN pip install --no-cache-dir \
    torch torchaudio \
    --index-url https://download.pytorch.org/whl/cpu

# Install remaining Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download the model at build time (baked into image, ~2.4GB)
ARG HF_TOKEN
RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download('ai4bharat/indic-conformer-600m-multilingual', token='${HF_TOKEN}')"

# Copy application code
COPY vexyl_stt_server.py .

# Cloud Run injects PORT; set defaults for local testing
ENV PORT=8080 \
    VEXYL_STT_HOST=0.0.0.0 \
    VEXYL_STT_DEVICE=cpu

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health')"

CMD ["python", "-u", "vexyl_stt_server.py"]
