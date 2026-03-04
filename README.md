<p align="center">
  <a href="https://vexyl.ai/">
    <img src="https://vexyl.ai/wp-content/themes/theme/assets/images/logo.png" alt="VEXYL AI" width="200">
  </a>
</p>

<h1 align="center">VEXYL-STT</h1>

<p align="center"><strong>Open-source Indian language speech-to-text server</strong></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.10%2B-green.svg" alt="Python 3.10+"></a>
  <a href="#supported-languages"><img src="https://img.shields.io/badge/Languages-14-orange.svg" alt="Languages"></a>
</p>

WebSocket + REST speech-to-text server wrapping the [ai4bharat/indic-conformer-600m-multilingual](https://huggingface.co/ai4bharat/indic-conformer-600m-multilingual) model (600M parameters). Self-hosted, zero API costs, full data sovereignty.

Built by [VEXYL AI](https://vexyl.ai/) — the team behind the **AI Voice Gateway**, an enterprise platform that bridges telephony (PSTN, SIP, Asterisk, WebRTC) with LLMs and AI services. VEXYL-STT is the open-source STT component, extracted for standalone use and community contribution.

---

## Overview

VEXYL-STT provides two transcription modes on a single port:

- **Real-time streaming** — WebSocket connection with energy-based VAD, accepts 16kHz 16-bit mono PCM audio, returns JSON transcripts in real time
- **Batch transcription** — REST API for async file-based transcription (WAV, MP3, FLAC, OGG, M4A). Upload a file, poll for results

### Features

- 14 Indian languages supported
- Energy-based VAD (no external VAD dependency)
- WebSocket streaming + batch REST API on the same port
- API key authentication (optional)
- Docker and Cloud Run ready
- Browser test clients included


---

## Supported Languages

| Code | Language | Code | Language |
|------|----------|------|----------|
| `ml-IN` | Malayalam | `mr-IN` | Marathi |
| `hi-IN` | Hindi | `pa-IN` | Punjabi |
| `ta-IN` | Tamil | `or-IN` | Odia |
| `te-IN` | Telugu | `as-IN` | Assamese |
| `kn-IN` | Kannada | `ur-IN` | Urdu |
| `bn-IN` | Bengali | `sa-IN` | Sanskrit |
| `gu-IN` | Gujarati | `ne-IN` | Nepali |

---

## Quick Start

```bash
# 1. Run the automated setup (one command)
./setup.sh

# 2. Start the server
./run.sh

# 3. Test in browser
open test.html
```

### Prerequisites

- **Python 3.10+**
- **macOS or Linux**
- **HuggingFace account** with access approved for the [gated model](https://huggingface.co/ai4bharat/indic-conformer-600m-multilingual)
- **~3 GB disk space** for model weights and dependencies

The setup script handles everything: creates a virtual environment, installs dependencies, authenticates with HuggingFace, downloads the model, and generates config files.

---

## Manual Setup

### 1. Create virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
```

### 2. Install dependencies

```bash
# PyTorch (CPU-only, smaller download)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu

# Other dependencies
pip install transformers websockets numpy onnxruntime soundfile
```

For GPU acceleration:

```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
```

### 3. Authenticate with HuggingFace

```bash
pip install huggingface_hub
huggingface-cli login
```

You need a token from [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) with read access. Request access to the model at [huggingface.co/ai4bharat/indic-conformer-600m-multilingual](https://huggingface.co/ai4bharat/indic-conformer-600m-multilingual).

### 4. Download the model

```bash
python3 -c "
from transformers import AutoModel
AutoModel.from_pretrained('ai4bharat/indic-conformer-600m-multilingual', trust_remote_code=True)
"
```

### 5. Create `.env`

```bash
VEXYL_STT_HOST=127.0.0.1
VEXYL_STT_PORT=8091
VEXYL_STT_DECODE=ctc
VEXYL_STT_DEVICE=cpu
# VEXYL_STT_API_KEY=your-secret-here
```

### 6. Start the server

```bash
source venv/bin/activate
export $(grep -v '^#' .env | xargs)
python3 vexyl_stt_server.py
```

---

## Configuration

### Environment Variables

| Variable | Default | Options | Description |
|----------|---------|---------|-------------|
| `VEXYL_STT_HOST` | `0.0.0.0` | Any IP | Bind address. Use `127.0.0.1` for local-only |
| `VEXYL_STT_PORT` | `8080` | Any port | Port number (via `PORT` or `VEXYL_STT_PORT`). The sample `.env` uses `8091` |
| `VEXYL_STT_DECODE` | `ctc` | `ctc`, `rnnt` | Decoding mode. CTC is faster, RNNT is more accurate |
| `VEXYL_STT_DEVICE` | `auto` | `auto`, `cpu`, `cuda` | Inference device. `auto` uses CUDA if available |
| `VEXYL_STT_MAX_CONN` | `50` | Any integer | Max concurrent WebSocket connections |
| `VEXYL_STT_API_KEY` | _(empty)_ | Any string | Shared secret for authentication. Clients must send `X-API-Key` header |

### API Key Authentication

Set `VEXYL_STT_API_KEY` on both server and client. The client sends the key as an `X-API-Key` header. The `/health` endpoint is always exempt. When the variable is empty, authentication is disabled.

```bash
# Server .env
VEXYL_STT_API_KEY=your-shared-secret

# Test with wscat
wscat -c ws://127.0.0.1:8091 -H "X-API-Key: your-shared-secret"
```

---

## API Reference

### WebSocket Protocol

```
Client                              Server
  │                                    │
  │◄──── {"type":"ready"} ─────────────│  (immediate on connect)
  │── {"type":"start",...} ───────────►│  (begin session)
  │◄──── {"type":"started",...} ───────│
  │── [binary PCM audio] ────────────►│  (stream 16kHz 16-bit mono PCM)
  │◄──── {"type":"final",...} ─────────│  (VAD triggers transcription)
  │── {"type":"stop"} ───────────────►│  (end session)
  │◄──── {"type":"final",...} ─────────│  (flush remaining audio)
  │◄──── {"type":"stopped"} ──────────│
```

#### Client → Server

| Message | Description |
|---------|-------------|
| `{"type":"start","lang":"ml-IN","session_id":"abc"}` | Begin transcription session |
| `[binary]` | Raw 16kHz 16-bit mono PCM audio bytes |
| `{"type":"stop"}` | End session (flushes buffered audio) |
| `{"type":"ping"}` | Keepalive |

#### Server → Client

| Message | Description |
|---------|-------------|
| `{"type":"ready","model":"..."}` | Server loaded, ready for sessions |
| `{"type":"started","session_id":"...","lang":"..."}` | Session begun |
| `{"type":"final","text":"...","lang":"...","duration":2.45,"latency_ms":320}` | Transcription result |
| `{"type":"stopped"}` | Session ended |
| `{"type":"pong"}` | Keepalive response |
| `{"type":"error","message":"..."}` | Error |

### Batch API

```
POST /batch/transcribe       → submit audio file
GET  /batch/status/{job_id}  → check job status
GET  /batch/result/{job_id}  → get transcript (202 if not ready)
GET  /health                 → health check
```

#### Submit — `POST /batch/transcribe`

```bash
curl -X POST http://localhost:8091/batch/transcribe \
  -H "X-API-Key: your-secret" \
  -F "file=@recording.wav" \
  -F "language_code=hi-IN"
```

Response (201):
```json
{"job_id": "batch_a1b2c3d4", "status": "queued", "language": "hi-IN", "audio_duration": 4.52}
```

#### Status — `GET /batch/status/{job_id}`

Returns job status with transcript when completed.

#### Result — `GET /batch/result/{job_id}`

Returns 202 if processing, 200 when complete.

#### Limits

| Limit | Value |
|-------|-------|
| Max file size | 25 MB |
| Max audio duration | 5 minutes |
| Max pending jobs | 1,000 |
| Job TTL | 1 hour |
| Supported formats | WAV, MP3, FLAC, OGG, M4A |

### Health Endpoint

```bash
curl http://127.0.0.1:8091/health
```

```json
{
  "status": "ok",
  "model": "indic-conformer-600m-multilingual",
  "device": "cpu",
  "decode_mode": "ctc",
  "active_sessions": 0,
  "max_connections": 50,
  "uptime_seconds": 3600.5,
  "batch_jobs_queued": 0,
  "batch_jobs_total": 0
}
```

---

## Browser Test Clients

### `test.html` — Real-time streaming

Open directly in a browser. Records from microphone, streams to server, displays transcripts in real time.

### `test-batch.html` — Batch transcription

Upload audio files or record from microphone for async batch transcription.

---

## Docker

### Build

```bash
docker build --build-arg HF_TOKEN=$HF_TOKEN -t vexyl-stt .
```

### Run

```bash
docker run -p 8080:8080 vexyl-stt

# With API key
docker run -p 8080:8080 -e VEXYL_STT_API_KEY=mysecret vexyl-stt
```

---

## Cloud Run Deployment

See [DEPLOY.md](DEPLOY.md) for a complete guide.

Quick deploy:

```bash
export GCP_PROJECT_ID=your-project-id
export HF_TOKEN=hf_your_token
./deploy.sh
```

---

## VEXYL AI Voice Gateway

[VEXYL AI Voice Gateway](https://vexyl.ai/) is an enterprise platform that connects phone calls directly to AI — bridging traditional telephony (PSTN, SIP, Asterisk, WebRTC) with LLMs, STT, and TTS providers. It supports 17+ AI providers including OpenAI, Groq, Deepgram, and ElevenLabs, with sub-200ms latency and features like barge-in, human escalation, and outbound calling.

VEXYL-STT plugs into the Voice Gateway as a self-hosted STT provider, giving you Indian language transcription with zero external API calls — ideal for data sovereignty, cost control, or as a fallback when cloud STT providers are unavailable.

**Key benefits of using VEXYL-STT with the Voice Gateway:**
- **Zero API cost** for Indian language calls — no per-minute STT billing
- **Full data sovereignty** — audio never leaves your infrastructure
- **Fallback resilience** — automatic failover from cloud STT to local model
- **Low latency** — same-machine WebSocket connection, no network round-trip

Visit [vexyl.ai](https://vexyl.ai/) to learn more about the enterprise product.

### Voice Gateway Client Library

The `vexyl-stt-client.js` module provides a Node.js client that follows the same interface pattern as other Voice Gateway STT providers (Groq, Deepgram, Sarvam, etc.).

```javascript
const { VexylSTT } = require('./vexyl-stt-client.js');

const stt = new VexylSTT('ml-IN');
stt.onTranscript = (text) => console.log('Transcript:', text);
stt.onError = (err) => console.error('Error:', err);

await stt.connect();
stt.sendAudio(pcmBuffer);  // 16kHz 16-bit mono PCM
await stt.stop();
```

**Environment variables:**
- `VEXYL_STT_URL` — WebSocket URL (default: `ws://127.0.0.1:8091`)
- `VEXYL_STT_API_KEY` — Shared secret for `X-API-Key` header

See `stt-provider-patch.md` and `language-config-patch.md` for Voice Gateway integration instructions.

---

## Production

### PM2

```bash
pm2 start run.sh --name vexyl-stt
pm2 logs vexyl-stt
pm2 save && pm2 startup
```

### GPU Acceleration

```bash
source venv/bin/activate
pip uninstall torch torchaudio -y
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
```

Set `VEXYL_STT_DEVICE=auto` (or `cuda`) in `.env` and restart.

### RNNT Decoding

For higher accuracy at the cost of slightly increased latency:

```bash
VEXYL_STT_DECODE=rnnt
```

---

## Troubleshooting

### HuggingFace 401/403

Verify your token (`huggingface-cli whoami`) and ensure you have been granted access to the [gated model](https://huggingface.co/ai4bharat/indic-conformer-600m-multilingual).

### Port already in use

```bash
lsof -i :8091
# Or change port: VEXYL_STT_PORT=8092
```

### No transcription output

- Check audio format: server expects 16kHz, 16-bit, mono PCM
- Check language code: unknown codes default to Malayalam (`ml`)
- Check VAD threshold: quiet audio may not exceed `SILENCE_THRESHOLD` (0.015)

### WebSocket connection refused

- Ensure the server is running: `./run.sh`
- Check host/port match between client and `.env`
- For remote access, set `VEXYL_STT_HOST=0.0.0.0`

---

## Project Files

| File | Description |
|------|-------------|
| `vexyl_stt_server.py` | Python server — WebSocket streaming + batch REST API |
| `vexyl-stt-client.js` | Node.js client library for Voice Gateway integration |
| `setup.sh` | Automated setup — venv, deps, HuggingFace auth, model download |
| `run.sh` | Start script — loads `.env`, activates venv, launches server |
| `deploy.sh` | One-command Cloud Run deployment |
| `Dockerfile` | Container image with baked-in model |
| `.env.example` | Template for server configuration |
| `test.html` | Browser test client for real-time streaming |
| `test-batch.html` | Browser test client for batch API |
| `stt-provider-patch.md` | Voice Gateway `stt-provider.js` integration guide |
| `language-config-patch.md` | Voice Gateway `language-config.js` integration guide |

---

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.

---

## License

[Apache License 2.0](LICENSE) — Copyright 2025 VEXYL AI
