# Deploying VEXYL-STT to Google Cloud Run

Complete guide for deploying the VEXYL-STT server to Google Cloud Run as a serverless container.

VEXYL-STT is the open-source STT component from [VEXYL AI](https://vexyl.ai/), the enterprise AI Voice Gateway platform. When deployed to Cloud Run, it provides a scalable, serverless Indian language transcription service that can be used standalone or integrated with the [VEXYL AI Voice Gateway](https://vexyl.ai/) for production telephony workloads.

---

## What Gets Deployed

A single container exposing a single port (8080) with two interfaces:

```
                        ┌──────────────────────────────────────┐
                        │     Cloud Run Container (port 8080)  │
                        │                                      │
  WebSocket clients ──► │  WebSocket  ─► Real-time streaming   │
  (VEXYL, test.html)    │  /           STT with VAD            │
                        │                                      │
  REST clients ───────► │  POST /batch/transcribe ─► Async     │
  (curl, apps)          │  GET  /batch/status/{id}   batch     │
                        │  GET  /batch/result/{id}   STT       │
                        │                                      │
  Health probes ──────► │  GET  /health                        │
                        └──────────────────────────────────────┘
```

- **WebSocket**: Real-time streaming transcription with energy-based VAD. Clients send 16kHz 16-bit mono PCM audio chunks and receive JSON transcripts as speech is detected.
- **Batch REST API**: Submit audio files (WAV, MP3, FLAC, OGG, M4A) and poll for results. Handles cold starts gracefully since clients don't hold an open connection.
- **Model**: [ai4bharat/indic-conformer-600m-multilingual](https://huggingface.co/ai4bharat/indic-conformer-600m-multilingual) — supports 14 Indian languages.

### Why Cloud Run

- **Scale to zero**: Pay nothing when there's no traffic ($0 idle)
- **Batch API handles cold starts**: Job submission returns instantly with a job ID; the model loads in the background and processes the job — callers never experience the 60-90s cold start directly
- **asia-south1 (Mumbai)**: Low latency for Indian users, which is the primary audience for Indian language STT

---

## Prerequisites

1. **Google Cloud SDK** installed and authenticated
   ```bash
   gcloud auth login
   gcloud config set project YOUR_PROJECT_ID
   ```

2. **GCP project** with billing enabled

3. **HuggingFace token** with access to the gated model
   - Create a token at https://huggingface.co/settings/tokens
   - Request access at https://huggingface.co/ai4bharat/indic-conformer-600m-multilingual (click "Request access" — usually approved within minutes)

---

## Quick Deploy

Three commands:

```bash
export GCP_PROJECT_ID=your-gcp-project-id
export HF_TOKEN=hf_your_huggingface_token
./deploy.sh
```

The script handles everything: enabling APIs, creating the Artifact Registry, building the Docker image in the cloud, and deploying to Cloud Run. It prints the service URL when done.

---

## What `deploy.sh` Does

### Step 1: Enable GCP APIs

```bash
gcloud services enable cloudbuild.googleapis.com run.googleapis.com artifactregistry.googleapis.com
```

Enables Cloud Build (remote Docker builds), Cloud Run (serverless containers), and Artifact Registry (Docker image storage).

### Step 2: Create Artifact Registry Docker Repository

```bash
gcloud artifacts repositories create vexyl-stt --repository-format=docker --location=asia-south1
```

Creates a Docker registry at `asia-south1-docker.pkg.dev/PROJECT_ID/vexyl-stt/`. Skips creation if it already exists.

### Step 3: Build Docker Image via Cloud Build

```bash
gcloud builds submit . --tag=IMAGE_URI --build-arg=HF_TOKEN=... --machine-type=e2-highcpu-8 --timeout=3600s
```

Uploads the source to Cloud Build and builds the Docker image remotely on an `e2-highcpu-8` machine. Takes **~15-20 minutes** because the image includes PyTorch and downloads the ~2.4 GB model at build time. The HF_TOKEN is passed as a build argument so the model can be downloaded from the gated HuggingFace repository.

### Step 4: Deploy to Cloud Run

Deploys the built image with WebSocket-optimized settings (see configuration table below). Cloud Run performs a zero-downtime rolling update if the service already exists.

---

## Cloud Run Configuration

Every setting in `deploy.sh` was chosen for a reason:

| Setting | Value | Why |
|---------|-------|-----|
| `--cpu` | 2 vCPUs | Model inference is CPU-bound; 2 vCPUs handles concurrent requests |
| `--memory` | 4 GiB | Model is ~2.4 GB in memory; 4 GiB gives headroom for audio buffers and batch jobs |
| `--timeout` | 3600s (1 hour) | Maximum allowed; needed for long WebSocket sessions |
| `--concurrency` | 50 | Matches `VEXYL_STT_MAX_CONN` default in the server |
| `--min-instances` | 0 | Scale to zero when idle — $0 when not in use |
| `--max-instances` | 5 | Cost cap; prevents runaway scaling. 5 × 50 concurrency = 250 simultaneous connections |
| `--cpu-boost` | Enabled | Temporarily allocates extra CPU during startup — faster cold starts, no extra cost |
| `--session-affinity` | Enabled | Routes a client's requests to the same instance — required for WebSocket stickiness |
| `--no-cpu-throttling` | Enabled | CPU stays allocated between requests; without this, WebSocket connections would be throttled when not actively sending data |
| `--startup-probe-path` | `/health` | Cloud Run checks `/health` to know when the container is ready |
| `--startup-probe-period` | 10s | Check every 10 seconds during startup |
| `--startup-probe-failure-threshold` | 12 | Allow up to 120s (12 × 10s) for model loading + warm-up inference |
| `--liveness-probe-path` | `/health` | Ongoing health monitoring after startup |
| `--allow-unauthenticated` | Enabled | No IAM auth — access control is handled by the application-level API key instead |

---

## Environment Variables

### Required (for deployment)

| Variable | Description |
|----------|-------------|
| `GCP_PROJECT_ID` | Your Google Cloud project ID |
| `HF_TOKEN` | HuggingFace access token for the gated model |

### Optional (override deploy.sh defaults)

| Variable | Default | Description |
|----------|---------|-------------|
| `GCP_REGION` | `asia-south1` | GCP region (Mumbai — low latency for India) |
| `SERVICE_NAME` | `vexyl-stt` | Cloud Run service name |
| `REPO_NAME` | `vexyl-stt` | Artifact Registry repository name |

### Runtime (set in container / Cloud Run)

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Server port (Cloud Run injects this automatically) |
| `VEXYL_STT_HOST` | `0.0.0.0` | Bind address |
| `VEXYL_STT_DEVICE` | `cpu` | Inference device (`cpu` or `cuda`) |
| `VEXYL_STT_DECODE` | `ctc` | Decode mode: `ctc` = faster, `rnnt` = more accurate |
| `VEXYL_STT_MAX_CONN` | `50` | Max concurrent WebSocket connections |
| `VEXYL_STT_API_KEY` | _(empty)_ | Shared secret for API key auth (see below) |

---

## API Key Authentication

By default, any client that knows the Cloud Run URL can connect. To restrict access, set a shared secret via the `VEXYL_STT_API_KEY` environment variable on **both** the server and the client.

### How it works

- The client sends the key as an `X-API-Key` HTTP header (on WebSocket upgrade and on REST API calls)
- The server validates the header using timing-safe comparison (`hmac.compare_digest`)
- If the key is missing or wrong → **HTTP 403 Forbidden**
- The `/health` endpoint is **exempt** — Cloud Run startup/liveness probes always work without a key
- When `VEXYL_STT_API_KEY` is **not set** (or empty), all connections are allowed (backwards compatible for local dev)

### Setting the key on Cloud Run

```bash
# Generate a strong random key
export API_KEY=$(openssl rand -base64 32)

# Set it on the Cloud Run service
gcloud run services update vexyl-stt \
    --region=asia-south1 \
    --set-env-vars VEXYL_STT_API_KEY=$API_KEY

# Print it so you can add it to your VEXYL .env
echo "VEXYL_STT_API_KEY=$API_KEY"
```

### Setting the key on the VEXYL client

Add to your VEXYL `.env` file:

```env
VEXYL_STT_URL=wss://vexyl-stt-XXXX-el.a.run.app
VEXYL_STT_API_KEY=your-shared-secret-here
```

The Node.js client (`vexyl-stt.js`) reads this env var and sends it as the `X-API-Key` header on every WebSocket connection.

### Verifying

```bash
# Health check — works without a key (exempt)
curl https://vexyl-stt-XXXX-el.a.run.app/health

# WebSocket without key — should get 403
wscat -c wss://vexyl-stt-XXXX-el.a.run.app
# Expected: error: Unexpected server response: 403

# WebSocket with correct key — should connect and receive ready message
wscat -c wss://vexyl-stt-XXXX-el.a.run.app -H "X-API-Key: your-shared-secret-here"
# Expected: {"type":"ready","model":"indic-conformer-600m-multilingual"}
```

---

## Post-Deployment Verification

The deploy script prints the service URL. Use it to verify everything works:

### 1. Health check

```bash
curl https://vexyl-stt-XXXX-el.a.run.app/health
```

Expected response:

```json
{
  "status": "ok",
  "model": "indic-conformer-600m-multilingual",
  "device": "cpu",
  "decode_mode": "ctc",
  "active_sessions": 0,
  "max_connections": 50,
  "uptime_seconds": 42.3,
  "batch_jobs_queued": 0,
  "batch_jobs_total": 0
}
```

### 2. Batch API test (submit → poll → result)

```bash
# Submit a transcription job
curl -X POST https://vexyl-stt-XXXX-el.a.run.app/batch/transcribe \
  -H "X-API-Key: your-shared-secret-here" \
  -F "file=@test_audio.wav" \
  -F "language_code=hi-IN"
# → {"job_id":"batch_a1b2c3d4e5f6g7h8","status":"queued","language":"hi-IN","audio_duration":4.52}

# Poll status (replace with actual job_id)
curl https://vexyl-stt-XXXX-el.a.run.app/batch/status/batch_a1b2c3d4e5f6g7h8 \
  -H "X-API-Key: your-shared-secret-here"
# → {"job_id":"...","status":"completed","transcript":"...","latency_ms":320,...}

# Get result directly (returns 202 if still processing, 200 when done)
curl https://vexyl-stt-XXXX-el.a.run.app/batch/result/batch_a1b2c3d4e5f6g7h8 \
  -H "X-API-Key: your-shared-secret-here"
```

### 3. WebSocket test

Open `test.html` in a browser, update the WebSocket URL to `wss://vexyl-stt-XXXX-el.a.run.app`, and test with your microphone.

Or use `wscat`:

```bash
wscat -c wss://vexyl-stt-XXXX-el.a.run.app -H "X-API-Key: your-shared-secret-here"
```

### 4. VEXYL integration

Set in your VEXYL `.env`:

```env
VEXYL_STT_URL=wss://vexyl-stt-XXXX-el.a.run.app
VEXYL_STT_API_KEY=your-shared-secret-here
```

---

## Batch API Reference

### POST /batch/transcribe

Submit an audio file for asynchronous transcription.

**Request**: `multipart/form-data`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | file | Yes | Audio file (.wav, .mp3, .flac, .ogg, .m4a) |
| `language_code` | string | No | Language code (default: `hi-IN`). See supported languages below |

**Response** (201 Created):

```json
{
  "job_id": "batch_a1b2c3d4e5f6g7h8",
  "status": "queued",
  "language": "hi-IN",
  "audio_duration": 4.52
}
```

### GET /batch/status/{job_id}

Check the status of a transcription job.

**Response when queued/processing** (200):

```json
{
  "job_id": "batch_a1b2c3d4e5f6g7h8",
  "status": "processing",
  "language": "hi-IN",
  "duration": 4.52,
  "created_at": 1709500000.0
}
```

**Response when completed** (200):

```json
{
  "job_id": "batch_a1b2c3d4e5f6g7h8",
  "status": "completed",
  "language": "hi-IN",
  "duration": 4.52,
  "created_at": 1709500000.0,
  "transcript": "नमस्ते दुनिया",
  "latency_ms": 320,
  "completed_at": 1709500003.2
}
```

**Response when failed** (200):

```json
{
  "job_id": "batch_a1b2c3d4e5f6g7h8",
  "status": "failed",
  "language": "hi-IN",
  "duration": 4.52,
  "created_at": 1709500000.0,
  "error_message": "Transcription failed",
  "completed_at": 1709500002.1
}
```

### GET /batch/result/{job_id}

Get the transcription result. Returns **202 Accepted** if still processing, **200 OK** when complete.

**Response when still processing** (202):

```json
{
  "job_id": "batch_a1b2c3d4e5f6g7h8",
  "status": "processing",
  "language": "hi-IN",
  "duration": 4.52
}
```

**Response when completed** (200):

```json
{
  "job_id": "batch_a1b2c3d4e5f6g7h8",
  "status": "completed",
  "transcript": "नमस्ते दुनिया",
  "language": "hi-IN",
  "duration": 4.52,
  "latency_ms": 320
}
```

### Error Codes

| Code | Condition |
|------|-----------|
| 400 | Missing file, unsupported format, invalid Content-Type, audio too long, audio decode failure |
| 403 | Invalid or missing API key |
| 404 | Job not found (invalid job_id) |
| 413 | File exceeds 25 MB size limit |
| 429 | Too many pending jobs (max 1,000 queued/processing) |

### Limits

| Limit | Value |
|-------|-------|
| Max file size | 25 MB |
| Max audio duration | 5 minutes (300s) |
| Max pending jobs | 1,000 |
| Job result TTL | 1 hour (expired jobs are cleaned up every 5 minutes) |

### Supported Audio Formats

| Format | Handled by |
|--------|-----------|
| WAV, FLAC, OGG, AIFF | `soundfile` (libsndfile) — native, no extra deps |
| MP3, M4A | `ffmpeg` subprocess fallback — ffmpeg is included in the Docker image |

All audio is automatically converted to 16kHz mono float32 PCM before inference. Stereo files are mixed down to mono. Non-16kHz files are resampled.

### Supported Languages

| Code | Language | Code | Language |
|------|----------|------|----------|
| `hi-IN` | Hindi | `pa-IN` | Punjabi |
| `ml-IN` | Malayalam | `or-IN` | Odia |
| `ta-IN` | Tamil | `as-IN` | Assamese |
| `te-IN` | Telugu | `ur-IN` | Urdu |
| `kn-IN` | Kannada | `sa-IN` | Sanskrit |
| `bn-IN` | Bengali | `ne-IN` | Nepali |
| `gu-IN` | Gujarati | `mr-IN` | Marathi |

Short codes (e.g., `hi`, `ml`, `ta`) are also accepted.

---

## Local Docker Testing

### Build

```bash
docker build --build-arg HF_TOKEN=$HF_TOKEN -t vexyl-stt .
```

### Run

```bash
# Without API key (open access for local dev)
docker run -p 8080:8080 vexyl-stt

# With API key (same as production)
docker run -p 8080:8080 -e VEXYL_STT_API_KEY=mysecret vexyl-stt
```

### Test

```bash
# Health check
curl http://localhost:8080/health

# Batch: submit job
curl -X POST http://localhost:8080/batch/transcribe \
  -F "file=@test_audio.wav" -F "language_code=hi-IN"
# → {"job_id":"batch_...","status":"queued","language":"hi-IN","audio_duration":4.52}

# Batch: check result (replace with actual job_id)
curl http://localhost:8080/batch/status/batch_...

# WebSocket: open test.html in browser → set URL to ws://localhost:8080
```

---

## Docker Image Details

| Layer | Size (approx) | Purpose |
|-------|---------------|---------|
| `python:3.11-slim` base | ~150 MB | Minimal Python runtime |
| System deps (`libgomp1`, `libsndfile1`, `ffmpeg`) | ~50 MB | OpenMP threading, audio I/O, codec support |
| PyTorch CPU + torchaudio | ~800 MB | Inference engine |
| Python dependencies (`requirements.txt`) | ~200 MB | transformers, websockets, numpy, soundfile, etc. |
| STT model | ~2.4 GB | Baked in at build time |
| **Total** | **~3.5-4 GB** | |

**Why the model is baked into the image**: Avoids downloading ~2.4 GB on every cold start. Without this, each scale-from-zero event would add 2-5 minutes of download time on top of the model loading time.

**Why ffmpeg is included**: The batch API accepts MP3 and M4A files, which `soundfile` (libsndfile) cannot decode natively. `ffmpeg` handles these formats via a subprocess fallback.

---

## Cost Analysis

### Pricing Model

Cloud Run bills per-second for CPU and memory while a container instance is active:

| Resource | Price (asia-south1) |
|----------|-------------------|
| vCPU-second | $0.00002400 |
| GiB-second | $0.00000250 |

With `--min-instances=0`, you pay **$0 when idle** — no traffic, no cost.

### Per-Request Cost

For a single batch transcription request that takes 10 seconds of processing time on a 2-vCPU / 4-GiB instance:

| Resource | Calculation | Cost |
|----------|------------|------|
| CPU | 2 vCPU × 10s × $0.000024 | $0.00048 |
| Memory | 4 GiB × 10s × $0.0000025 | $0.00010 |
| **Total per request** | | **~$0.0006** |

### Monthly Cost Estimates

| Usage | Requests/month | Active seconds | Estimated cost |
|-------|---------------|----------------|---------------|
| Light (testing/dev) | ~100 | ~1,000s | **~$0.06** |
| Medium (internal tool) | ~1,000 | ~10,000s | **~$0.60** |
| Heavy (production) | ~10,000 | ~100,000s | **~$6.00** |
| Always-warm (min-instances=1) | any | 2,592,000s | **~$50-70/month** |

### Other Costs

- **Cloud Build**: ~$0.50 per build (`e2-highcpu-8` for ~20 min)
- **Artifact Registry**: ~$0.10/GB/month for image storage (~4 GB = ~$0.40/month)
- **Free tier**: Cloud Run includes 180,000 vCPU-seconds and 360,000 GiB-seconds free per month, which covers most light-to-medium usage entirely

---

## Cost Optimization Tips

1. **Batch-only mode**: If you only use the batch API (no WebSocket), you can remove `--session-affinity` and `--no-cpu-throttling` from `deploy.sh` to let Cloud Run throttle CPU between requests — reducing cost for sporadic usage.

2. **CPU boost is free**: `--cpu-boost` is already enabled. It gives extra CPU during startup at no additional charge, reducing cold start time.

3. **asia-south1 for India**: Already the default. Changing to a US/EU region adds 150-300ms latency for Indian users.

4. **Dev vs production instances**:
   - Development: `--min-instances=0` (default) — scale to zero, pay nothing idle
   - Production: `--min-instances=1` — always-warm, no cold starts (~$50-70/month)

5. **Reduce max instances**: `--max-instances=5` is the default cost cap. Lower it if you want tighter cost control.

---

## Updating the Service

### Code changes (rebuild + redeploy)

Re-run the deploy script:

```bash
./deploy.sh
```

Cloud Build rebuilds the image and Cloud Run performs a **zero-downtime rolling update** — new instances start, health checks pass, then old instances drain connections and shut down.

### Config-only changes (no rebuild)

To change Cloud Run settings without rebuilding the image:

```bash
gcloud run services update vexyl-stt \
    --region=asia-south1 \
    --memory=8Gi \
    --max-instances=10
```

### Environment variable changes

```bash
gcloud run services update vexyl-stt \
    --region=asia-south1 \
    --set-env-vars VEXYL_STT_API_KEY=new-key-here,VEXYL_STT_DECODE=rnnt
```

---

## Monitoring & Logs

### View recent logs

```bash
gcloud run services logs read vexyl-stt \
    --region=asia-south1 \
    --limit=100
```

### Check service status

```bash
gcloud run services describe vexyl-stt \
    --region=asia-south1
```

### Cloud Console

View metrics, logs, and configuration in the browser:

```
https://console.cloud.google.com/run/detail/asia-south1/vexyl-stt/metrics
```

---

## Troubleshooting

### Build fails with "model download error"

- Verify your `HF_TOKEN` is valid and has not expired
- Visit https://huggingface.co/ai4bharat/indic-conformer-600m-multilingual and click "Request access" if you haven't already
- Check Cloud Build logs: `gcloud builds list --project=PROJECT_ID`

### Cold start timeout

- The startup probe allows 120s (12 checks × 10s interval)
- If the model takes longer to load, increase the failure threshold in `deploy.sh`:
  ```
  --startup-probe-failure-threshold=18    # 180s
  ```

### WebSocket disconnects after ~1 hour

- Cloud Run has a maximum timeout of 3600s (already set)
- For longer sessions, implement reconnection logic in the client

### 403 Forbidden on WebSocket or batch API

- Check that the `X-API-Key` header value matches `VEXYL_STT_API_KEY` exactly
- The key is case-sensitive and must be an exact match
- `/health` is exempt from API key checks — if health works but other endpoints return 403, it's a key mismatch

### CORS errors in browser

- CORS headers (`Access-Control-Allow-Origin: *`) are already set on all HTTP responses and batch API responses
- If using a custom domain with a reverse proxy, ensure it forwards the CORS headers

### Out of memory

- The default 4 GiB should be sufficient, but if you see OOM kills in logs:
  ```bash
  gcloud run services update vexyl-stt --region=asia-south1 --memory=8Gi
  ```
