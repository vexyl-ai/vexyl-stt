"""
vexyl_stt_server.py
VEXYL-STT Server
------------------------------------------------------
Wraps ai4bharat/indic-conformer-600m-multilingual in a WebSocket server.
Accepts 16kHz 16-bit mono PCM audio chunks, returns transcripts as JSON.
Also exposes a Sarvam-style batch transcription API (POST /batch/transcribe).

Usage:
    pip install transformers torchaudio websockets numpy torch soundfile
    python vexyl_stt_server.py

Optional env vars:
    PORT                      (default: 8080, Cloud Run injects this)
    VEXYL_STT_HOST            (default: 0.0.0.0)
    VEXYL_STT_PORT            (fallback if PORT unset)
    VEXYL_STT_DECODE          (default: ctc)   options: ctc, rnnt
    VEXYL_STT_DEVICE          (default: auto)  options: auto, cpu, cuda
    VEXYL_STT_API_KEY         (default: empty) shared secret; if set, clients must send X-API-Key header
"""

import asyncio
import websockets
from websockets.asyncio.server import ServerConnection
import json
import numpy as np
import torch
import torchaudio
import os
import sys
import logging
import time
import signal
import threading
import io
import hmac
import uuid
import re
import soundfile as sf
from dataclasses import dataclass
from enum import Enum
from http import HTTPStatus
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [VexylSTT] %(levelname)s %(message)s"
)
log = logging.getLogger("vexyl_stt")

# ─── Config ────────────────────────────────────────────────────────────────────
HOST        = os.getenv("VEXYL_STT_HOST",   "0.0.0.0")
PORT        = int(os.getenv("PORT", os.getenv("VEXYL_STT_PORT", "8080")))
DECODE_MODE = os.getenv("VEXYL_STT_DECODE", "ctc")   # ctc = faster, rnnt = more accurate
DEVICE_PREF = os.getenv("VEXYL_STT_DEVICE", "auto")
API_KEY     = os.getenv("VEXYL_STT_API_KEY", "")

# Audio input: 16kHz 16-bit mono PCM
TARGET_SAMPLE_RATE = 16000

# VAD parameters - detect silence to trigger transcription
SILENCE_THRESHOLD    = 0.015   # RMS energy threshold
MIN_SPEECH_DURATION  = 0.3     # seconds of speech before attempting transcription
SILENCE_DURATION     = 0.6     # seconds of silence to consider utterance complete
MAX_BUFFER_DURATION  = 12.0    # force transcription after this many seconds

# Batch transcription config
BATCH_MAX_FILE_SIZE     = 25 * 1024 * 1024  # 25MB
BATCH_MAX_AUDIO_DURATION = 300.0             # 5 minutes
BATCH_MAX_JOBS          = 1000
BATCH_JOB_TTL           = 3600               # 1 hour

# Language code map — VEXYL language codes → model codes
LANG_MAP = {
    "ml-IN": "ml",  # Malayalam
    "hi-IN": "hi",  # Hindi
    "ta-IN": "ta",  # Tamil
    "te-IN": "te",  # Telugu
    "kn-IN": "kn",  # Kannada
    "bn-IN": "bn",  # Bengali
    "gu-IN": "gu",  # Gujarati
    "mr-IN": "mr",  # Marathi
    "pa-IN": "pa",  # Punjabi
    "or-IN": "or",  # Odia
    "as-IN": "as",  # Assamese
    "ur-IN": "ur",  # Urdu
    "sa-IN": "sa",  # Sanskrit
    "ne-IN": "ne",  # Nepali
    # Pass-through if already short code
    "ml": "ml", "hi": "hi", "ta": "ta", "te": "te",
    "kn": "kn", "bn": "bn", "gu": "gu", "mr": "mr",
    "pa": "pa", "or": "or", "as": "as", "ur": "ur",
    "sa": "sa", "ne": "ne",
}

# ─── Connection Limits ────────────────────────────────────────────────────────
MAX_CONNECTIONS = int(os.getenv("VEXYL_STT_MAX_CONN", "50"))
_conn_semaphore: asyncio.Semaphore  # initialized in main() (needs running loop)
active_sessions: dict[str, "STTSession"] = {}
_server_start_time: float = 0.0

# ─── Batch Job Types ─────────────────────────────────────────────────────────

class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class BatchJob:
    job_id: str
    status: JobStatus
    language: str
    created_at: float
    audio_pcm: Optional[np.ndarray] = None
    audio_duration: float = 0.0
    transcript: Optional[str] = None
    latency_ms: Optional[int] = None
    completed_at: Optional[float] = None
    error_message: Optional[str] = None

_batch_jobs: dict[str, BatchJob] = {}
_batch_queue: asyncio.Queue = None    # initialized in main()
_batch_worker_task: asyncio.Task = None
_batch_cleanup_task: asyncio.Task = None

# ─── Model Loader ──────────────────────────────────────────────────────────────
model = None
device = None
_infer_lock = threading.Lock()

def load_model():
    global model, device

    if DEVICE_PREF == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        device = DEVICE_PREF

    log.info(f"Loading ai4bharat/indic-conformer-600m-multilingual on {device}...")
    start = time.time()

    from transformers import AutoModel
    model = AutoModel.from_pretrained(
        "ai4bharat/indic-conformer-600m-multilingual",
        trust_remote_code=True
    )
    if device == "cuda":
        model = model.cuda()
    model.eval()

    elapsed = time.time() - start
    log.info(f"Model loaded in {elapsed:.1f}s on {device} | decode_mode={DECODE_MODE}")


# ─── VAD Helper ────────────────────────────────────────────────────────────────
def compute_rms(pcm_float32: np.ndarray) -> float:
    """Compute root-mean-square energy of audio chunk."""
    if len(pcm_float32) == 0:
        return 0.0
    return float(np.sqrt(np.mean(pcm_float32 ** 2)))


# ─── Audio Conversion ─────────────────────────────────────────────────────────

def _convert_audio_to_pcm_sync(audio_bytes: bytes) -> tuple[np.ndarray, float]:
    """Decode audio bytes (WAV/MP3/FLAC/OGG) to 16kHz mono float32 PCM.
    Uses soundfile (libsndfile) which supports WAV, FLAC, OGG, AIFF natively.
    For MP3/M4A, ffmpeg must be available on PATH as a subprocess fallback."""
    buf = io.BytesIO(audio_bytes)
    try:
        data, sample_rate = sf.read(buf, dtype="float32")
    except Exception:
        # soundfile can't handle MP3/M4A — fall back to ffmpeg subprocess
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix=".audio", delete=True) as tmp:
            tmp.write(audio_bytes)
            tmp.flush()
            result = subprocess.run(
                ["ffmpeg", "-i", tmp.name, "-f", "wav", "-acodec", "pcm_s16le",
                 "-ar", str(TARGET_SAMPLE_RATE), "-ac", "1", "-"],
                capture_output=True, timeout=60,
            )
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg failed: {result.stderr.decode(errors='replace')[:200]}")
            wav_buf = io.BytesIO(result.stdout)
            data, sample_rate = sf.read(wav_buf, dtype="float32")

    # Mono mixdown if stereo
    if data.ndim > 1:
        data = data.mean(axis=1)

    # Resample to 16kHz if needed
    if sample_rate != TARGET_SAMPLE_RATE:
        waveform = torch.from_numpy(data).unsqueeze(0)
        resampler = torchaudio.transforms.Resample(int(sample_rate), TARGET_SAMPLE_RATE)
        waveform = resampler(waveform)
        data = waveform.squeeze(0).numpy()

    pcm = data.astype(np.float32)
    duration = len(pcm) / TARGET_SAMPLE_RATE
    return pcm, duration


async def _convert_audio_to_pcm(audio_bytes: bytes) -> tuple[np.ndarray, float]:
    """Async wrapper — runs audio conversion in thread pool."""
    return await asyncio.to_thread(_convert_audio_to_pcm_sync, audio_bytes)


# ─── Multipart Parser ─────────────────────────────────────────────────────────

def _parse_multipart(content_type: str, body: bytes) -> dict:
    """Parse multipart/form-data. Returns dict of field_name → value or {filename, data}."""
    match = re.search(r'boundary=([^\s;]+)', content_type)
    if not match:
        return {}
    boundary = match.group(1).strip('"').encode()

    parts = body.split(b"--" + boundary)
    fields = {}

    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue

        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        headers_raw = part[:header_end].decode("utf-8", errors="replace")
        part_body = part[header_end + 4:]

        # Strip trailing \r\n left by boundary split
        if part_body.endswith(b"\r\n"):
            part_body = part_body[:-2]

        name_match = re.search(r'name="([^"]*)"', headers_raw)
        if not name_match:
            continue
        name = name_match.group(1)

        filename_match = re.search(r'filename="([^"]*)"', headers_raw)
        if filename_match:
            fields[name] = {"filename": filename_match.group(1), "data": part_body}
        else:
            fields[name] = part_body.decode("utf-8", errors="replace").strip()

    return fields


# ─── Batch Worker ──────────────────────────────────────────────────────────────

async def _batch_worker():
    """Background coroutine — pulls jobs from queue and runs inference.
    Wraps the loop in an outer try/except so unexpected errors don't kill the worker."""
    log.info("Batch worker started")
    while True:
        try:
            job_id = await _batch_queue.get()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("[batch] Error getting from queue", exc_info=True)
            await asyncio.sleep(1)
            continue

        try:
            job = _batch_jobs.get(job_id)
            if not job or job.status != JobStatus.QUEUED:
                continue

            job.status = JobStatus.PROCESSING
            log.info(f"[batch] Processing job {job_id} ({job.language}, {job.audio_duration:.1f}s)")

            start = time.time()
            text = await transcribe(job.audio_pcm, job.language)
            latency = int((time.time() - start) * 1000)

            job.transcript = text
            job.latency_ms = latency
            job.status = JobStatus.COMPLETED
            job.completed_at = time.time()
            job.audio_pcm = None  # free memory

            log.info(f"[batch] Job {job_id} completed: '{text}' ({latency}ms)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"[batch] Job {job_id} failed: {e}", exc_info=True)
            if job_id in _batch_jobs:
                _batch_jobs[job_id].status = JobStatus.FAILED
                _batch_jobs[job_id].error_message = "Transcription failed"
                _batch_jobs[job_id].audio_pcm = None
                _batch_jobs[job_id].completed_at = time.time()
        finally:
            try:
                _batch_queue.task_done()
            except ValueError:
                pass  # task_done called too many times


async def _batch_cleanup_loop():
    """Remove completed/failed jobs older than BATCH_JOB_TTL every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        expired = [
            jid for jid, job in _batch_jobs.items()
            if job.completed_at and (now - job.completed_at) > BATCH_JOB_TTL
        ]
        for jid in expired:
            del _batch_jobs[jid]
        if expired:
            log.info(f"[batch] Cleaned up {len(expired)} expired jobs")


# ─── Transcription ─────────────────────────────────────────────────────────────
def _run_inference(pcm_float32: np.ndarray, lang_code: str) -> str:
    """Synchronous inference — runs in thread pool so it doesn't block the event loop."""
    indic_lang = LANG_MAP.get(lang_code, "ml")
    wav = torch.from_numpy(pcm_float32).unsqueeze(0)
    if device == "cuda":
        wav = wav.cuda()
    with _infer_lock:
        with torch.no_grad():
            result = model(wav, indic_lang, DECODE_MODE)
    return result.strip() if isinstance(result, str) else str(result).strip()


async def transcribe(pcm_float32: np.ndarray, lang_code: str) -> str:
    """Run model inference off the event loop via asyncio.to_thread()."""
    if len(pcm_float32) == 0:
        return ""
    return await asyncio.to_thread(_run_inference, pcm_float32, lang_code)


# ─── Session Handler ───────────────────────────────────────────────────────────
class STTSession:
    """Manages audio buffering + VAD + transcription for one WebSocket connection."""

    def __init__(self, session_id: str, lang_code: str, websocket):
        self.session_id   = session_id
        self.lang_code    = lang_code
        self.websocket    = websocket
        self.audio_buffer = np.array([], dtype=np.float32)
        self.speech_active = False
        self.silence_frames = 0
        self.speech_frames  = 0
        self.total_buffered = 0.0  # seconds

        log.info(f"[{session_id}] Session started | lang={lang_code} | decode={DECODE_MODE}")

    def add_audio(self, pcm_bytes: bytes) -> None:
        """Ingest raw 16-bit PCM bytes (already at 16kHz)."""
        pcm_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        pcm_float = pcm_int16.astype(np.float32) / 32768.0
        self.audio_buffer = np.concatenate([self.audio_buffer, pcm_float])
        self.total_buffered = len(self.audio_buffer) / TARGET_SAMPLE_RATE

    def check_vad(self) -> str | None:
        """
        Check energy-based VAD on current buffer.
        Returns 'transcribe' if we should run STT, else None.
        """
        if len(self.audio_buffer) == 0:
            return None

        # Use last 100ms window for VAD decision
        window_size = int(0.1 * TARGET_SAMPLE_RATE)
        recent = self.audio_buffer[-window_size:] if len(self.audio_buffer) >= window_size else self.audio_buffer
        rms = compute_rms(recent)

        if rms > SILENCE_THRESHOLD:
            self.speech_active  = True
            self.silence_frames = 0
            self.speech_frames += 1
        else:
            if self.speech_active:
                self.silence_frames += 1

        speech_secs  = self.speech_frames * 0.1
        silence_secs = self.silence_frames * 0.1

        # Trigger: enough speech followed by silence
        if (self.speech_active and
                speech_secs >= MIN_SPEECH_DURATION and
                silence_secs >= SILENCE_DURATION):
            return "transcribe"

        # Force trigger: buffer too long
        if self.total_buffered >= MAX_BUFFER_DURATION:
            return "transcribe"

        return None

    async def process_if_ready(self) -> None:
        """Run transcription if VAD says so, then send result over WebSocket."""
        action = self.check_vad()
        if action != "transcribe":
            return
        if len(self.audio_buffer) < TARGET_SAMPLE_RATE * MIN_SPEECH_DURATION:
            return

        audio_to_transcribe  = self.audio_buffer.copy()
        duration             = len(audio_to_transcribe) / TARGET_SAMPLE_RATE

        # Reset buffer and VAD state
        self.audio_buffer   = np.array([], dtype=np.float32)
        self.speech_active  = False
        self.silence_frames = 0
        self.speech_frames  = 0
        self.total_buffered = 0.0

        start = time.time()
        text  = await transcribe(audio_to_transcribe, self.lang_code)
        latency = int((time.time() - start) * 1000)

        log.info(f"[{self.session_id}] Transcribed {duration:.1f}s → '{text}' ({latency}ms)")

        if text:
            await self.websocket.send(json.dumps({
                "type":      "final",
                "text":      text,
                "lang":      self.lang_code,
                "duration":  round(duration, 2),
                "latency_ms": latency
            }))

    async def flush(self) -> None:
        """Force transcribe any remaining audio on session stop."""
        if len(self.audio_buffer) < TARGET_SAMPLE_RATE * 0.2:
            return

        audio_to_transcribe = self.audio_buffer.copy()
        duration            = len(audio_to_transcribe) / TARGET_SAMPLE_RATE
        self.audio_buffer   = np.array([], dtype=np.float32)

        start = time.time()
        text  = await transcribe(audio_to_transcribe, self.lang_code)
        latency = int((time.time() - start) * 1000)

        log.info(f"[{self.session_id}] Flush transcribed {duration:.1f}s → '{text}' ({latency}ms)")

        if text:
            await self.websocket.send(json.dumps({
                "type":      "final",
                "text":      text,
                "lang":      self.lang_code,
                "duration":  round(duration, 2),
                "latency_ms": latency,
                "flushed":   True
            }))


# ─── WebSocket Handler ─────────────────────────────────────────────────────────
async def handle_connection(websocket):
    """
    Protocol:
      → Client sends JSON init:  {"type":"start","lang":"ml-IN","session_id":"abc"}
      → Client sends binary:     raw 16kHz 16-bit mono PCM bytes
      → Client sends JSON stop:  {"type":"stop"}
      ← Server sends JSON:       {"type":"final","text":"...","latency_ms":120}
      ← Server sends JSON:       {"type":"ready"} after model confirms loaded
      ← Server sends JSON:       {"type":"error","message":"..."}
    """
    session = None
    conn_id = f"conn_{id(websocket)}"
    remote  = websocket.remote_address

    try:
        await websocket.send(json.dumps({"type": "ready", "model": "indic-conformer-600m-multilingual"}))
        log.info(f"New connection from {remote}")

        async for message in websocket:

            # ── Binary audio chunk ──
            if isinstance(message, bytes):
                if session is None:
                    log.warning(f"{remote}: received audio before init message, ignoring")
                    continue
                session.add_audio(message)
                await session.process_if_ready()

            # ── JSON control message ──
            elif isinstance(message, str):
                try:
                    msg = json.loads(message)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({"type": "error", "message": "Invalid JSON"}))
                    continue

                msg_type = msg.get("type")

                if msg_type == "start":
                    lang        = msg.get("lang", "ml-IN")
                    session_id  = msg.get("session_id", f"sess_{int(time.time())}")
                    # Sanitize session_id (max 64 chars, strip non-printable)
                    session_id = re.sub(r'[^\w\-.]', '_', str(session_id))[:64]
                    # Validate language code
                    if lang not in LANG_MAP:
                        lang = "ml-IN"
                    session     = STTSession(session_id, lang, websocket)
                    active_sessions[conn_id] = session
                    await websocket.send(json.dumps({"type": "started", "session_id": session_id, "lang": lang}))

                elif msg_type == "stop":
                    if session:
                        await session.flush()
                        log.info(f"[{session.session_id}] Session stopped")
                    await websocket.send(json.dumps({"type": "stopped"}))
                    active_sessions.pop(conn_id, None)
                    session = None

                elif msg_type == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))

    except websockets.exceptions.ConnectionClosed:
        log.info(f"Connection closed: {remote}")
    except Exception as e:
        log.error(f"Handler error for {remote}: {e}", exc_info=True)
        try:
            await websocket.send(json.dumps({"type": "error", "message": "Internal server error"}))
        except Exception:
            pass
    finally:
        active_sessions.pop(conn_id, None)
        if session:
            try:
                await session.flush()
            except Exception:
                pass


async def _limited_handler(websocket):
    """Wrap handle_connection with a semaphore to cap concurrent connections."""
    if _conn_semaphore.locked() and _conn_semaphore._value == 0:
        await websocket.close(1013, "Server at capacity")
        log.warning(f"Rejected connection from {websocket.remote_address} — at capacity ({MAX_CONNECTIONS})")
        return
    async with _conn_semaphore:
        await handle_connection(websocket)


# ─── Batch-Capable Connection ────────────────────────────────────────────────
# websockets 16.x rejects POST requests at the HTTP/1.1 parsing level before
# _process_request() is ever called.  We subclass ServerConnection and override
# data_received() to intercept POST requests at the transport level.

class BatchCapableConnection(ServerConnection):
    """ServerConnection subclass that intercepts HTTP POST for batch endpoints."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._post_buffer = b""
        self._is_post: Optional[bool] = None  # None = undetermined
        self._handled_as_http = False

    async def handshake(self, *args, **kwargs):
        """Override to suppress the EOF error when we already handled as HTTP.
        The race: handshake() starts awaiting protocol data, then data_received()
        intercepts POST/OPTIONS and closes the transport, causing an EOF here."""
        try:
            return await super().handshake(*args, **kwargs)
        except Exception:
            if self._handled_as_http:
                return  # suppress — we already sent an HTTP response
            raise

    def data_received(self, data: bytes) -> None:
        # First chunk: determine request type
        if self._is_post is None:
            self._post_buffer = data
            if data[:7] == b"OPTIONS":
                self._handled_as_http = True
                self._send_cors_preflight()
                return
            elif data[:4] == b"POST":
                self._is_post = True
                self._handled_as_http = True
                self._try_handle_post()
                return
            else:
                self._is_post = False
                super().data_received(data)
                return

        if self._is_post:
            # Cap buffer to prevent unbounded memory growth
            # Allow headers (~8KB) + body (BATCH_MAX_FILE_SIZE)
            max_buffer = BATCH_MAX_FILE_SIZE + 64 * 1024
            if len(self._post_buffer) + len(data) > max_buffer:
                self._send_json_response(413, "Payload Too Large",
                                         {"error": "Request too large"})
                return
            self._post_buffer += data
            self._try_handle_post()
        else:
            super().data_received(data)

    def _try_handle_post(self):
        """Check if we have the full POST request, then handle it."""
        header_end = self._post_buffer.find(b"\r\n\r\n")
        if header_end == -1:
            return  # need more header data

        headers_section = self._post_buffer[:header_end]
        body_start = header_end + 4

        # Parse Content-Length (with validation)
        content_length = 0
        for line in headers_section.decode("utf-8", errors="replace").split("\r\n"):
            if line.lower().startswith("content-length:"):
                try:
                    content_length = int(line.split(":", 1)[1].strip())
                except (ValueError, IndexError):
                    self._send_json_response(400, "Bad Request",
                                             {"error": "Invalid Content-Length"})
                    return
                if content_length < 0 or content_length > BATCH_MAX_FILE_SIZE + 4096:
                    self._send_json_response(413, "Payload Too Large",
                                             {"error": f"Content-Length exceeds limit"})
                    return
                break

        body_so_far = self._post_buffer[body_start:]
        if len(body_so_far) < content_length:
            return  # need more body data

        # We have the full request
        body = body_so_far[:content_length]
        headers_raw = headers_section.decode("utf-8", errors="replace")
        task = asyncio.ensure_future(self._handle_post(headers_raw, body))
        task.add_done_callback(self._post_task_done)

    def _post_task_done(self, task: asyncio.Task):
        """Callback for POST handler task — log unhandled exceptions."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error(f"[batch] Unhandled POST handler error: {exc}", exc_info=exc)

    async def _handle_post(self, headers_raw: str, body: bytes):
        """Route and handle the POST request."""
        try:
            lines = headers_raw.split("\r\n")
            request_line = lines[0]  # e.g. "POST /batch/transcribe HTTP/1.1"
            parts = request_line.split(" ", 2)
            path = parts[1] if len(parts) > 1 else "/"

            # Parse headers into dict
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    key, val = line.split(":", 1)
                    headers[key.strip().lower()] = val.strip()

            # API key check (timing-safe)
            if API_KEY:
                client_key = headers.get("x-api-key", "")
                if not hmac.compare_digest(client_key, API_KEY):
                    self._send_json_response(403, "Forbidden",
                                             {"error": "Invalid or missing API key"})
                    return

            if path == "/batch/transcribe":
                await self._handle_batch_transcribe(headers, body)
            else:
                self._send_json_response(404, "Not Found",
                                         {"error": f"Unknown endpoint: {path}"})
        except Exception as e:
            log.error(f"[batch] POST handler error: {e}", exc_info=True)
            self._send_json_response(500, "Internal Server Error",
                                     {"error": "Internal server error"})

    async def _handle_batch_transcribe(self, headers: dict, body: bytes):
        """Handle POST /batch/transcribe — accept audio file for async transcription."""
        content_type = headers.get("content-type", "")

        if "multipart/form-data" not in content_type:
            self._send_json_response(400, "Bad Request",
                                     {"error": "Content-Type must be multipart/form-data"})
            return

        # Check file size before parsing
        if len(body) > BATCH_MAX_FILE_SIZE:
            self._send_json_response(413, "Payload Too Large",
                                     {"error": f"File exceeds {BATCH_MAX_FILE_SIZE // (1024*1024)}MB limit"})
            return

        fields = _parse_multipart(content_type, body)

        if "file" not in fields or not isinstance(fields["file"], dict):
            self._send_json_response(400, "Bad Request",
                                     {"error": "Missing 'file' field in multipart form"})
            return

        file_info = fields["file"]
        filename = file_info["filename"].lower()
        audio_data = file_info["data"]

        # Validate file extension
        supported_exts = (".wav", ".mp3", ".flac", ".ogg", ".m4a")
        if not any(filename.endswith(ext) for ext in supported_exts):
            self._send_json_response(400, "Bad Request",
                                     {"error": f"Unsupported format. Supported: {', '.join(supported_exts)}"})
            return

        # Check job limit
        pending_count = sum(1 for j in _batch_jobs.values()
                           if j.status in (JobStatus.QUEUED, JobStatus.PROCESSING))
        if pending_count >= BATCH_MAX_JOBS:
            self._send_json_response(429, "Too Many Requests",
                                     {"error": f"Too many pending jobs (max {BATCH_MAX_JOBS})"})
            return

        language_code = fields.get("language_code", "hi-IN")

        # Convert audio to PCM
        try:
            pcm, duration = await _convert_audio_to_pcm(audio_data)
        except Exception as e:
            log.error(f"[batch] Audio conversion failed: {e}", exc_info=True)
            self._send_json_response(400, "Bad Request",
                                     {"error": "Failed to decode audio file. Ensure the file is a valid audio format."})
            return

        if duration > BATCH_MAX_AUDIO_DURATION:
            self._send_json_response(400, "Bad Request",
                                     {"error": f"Audio too long ({duration:.1f}s). Max {BATCH_MAX_AUDIO_DURATION:.0f}s"})
            return

        # Create job
        job_id = f"batch_{uuid.uuid4().hex[:16]}"
        job = BatchJob(
            job_id=job_id,
            status=JobStatus.QUEUED,
            language=language_code,
            created_at=time.time(),
            audio_pcm=pcm,
            audio_duration=round(duration, 2),
        )
        _batch_jobs[job_id] = job
        await _batch_queue.put(job_id)

        log.info(f"[batch] Job {job_id} queued: {filename} ({duration:.1f}s, {language_code})")

        self._send_json_response(201, "Created", {
            "job_id": job_id,
            "status": "queued",
            "language": language_code,
            "audio_duration": round(duration, 2),
        })

    def _send_cors_preflight(self):
        """Respond to an OPTIONS preflight request."""
        response = (
            "HTTP/1.1 204 No Content\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            "Access-Control-Allow-Headers: Content-Type, X-API-Key\r\n"
            "Access-Control-Max-Age: 86400\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("utf-8")
        try:
            self.transport.write(response)
            self.transport.close()
        except Exception:
            pass

    def _send_json_response(self, status_code: int, status_text: str, body_dict: dict):
        """Write a raw HTTP JSON response to the transport and close."""
        body = json.dumps(body_dict).encode("utf-8")
        response = (
            f"HTTP/1.1 {status_code} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            f"Access-Control-Allow-Headers: Content-Type, X-API-Key\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8") + body
        try:
            self.transport.write(response)
            self.transport.close()
        except Exception:
            pass


# ─── CORS & HTTP Helpers ──────────────────────────────────────────────────────

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
}

def _json_response(connection, status: HTTPStatus, body_dict: dict):
    """Helper to build a JSON HTTP response via websockets' connection.respond()."""
    body = json.dumps(body_dict)
    response = connection.respond(status, body)
    response.headers["Content-Type"] = "application/json"
    for k, v in _CORS_HEADERS.items():
        response.headers[k] = v
    return response


def _process_request(connection, request):
    """Intercept HTTP requests before WebSocket upgrade.
    Serves /health, /batch/status/{id}, /batch/result/{id}.
    websockets 16.x API: (ServerConnection, Request) -> Response | None."""

    # ── Health check (no auth required) ──
    if request.path == "/health":
        queued = sum(1 for j in _batch_jobs.values() if j.status == JobStatus.QUEUED)
        return _json_response(connection, HTTPStatus.OK, {
            "status":           "ok",
            "model":            "indic-conformer-600m-multilingual",
            "device":           device,
            "decode_mode":      DECODE_MODE,
            "active_sessions":  len(active_sessions),
            "max_connections":  MAX_CONNECTIONS,
            "uptime_seconds":   round(time.time() - _server_start_time, 1),
            "batch_jobs_queued": queued,
            "batch_jobs_total":  len(_batch_jobs),
        })

    # API key check — skip if no key configured (backwards compatible for local dev)
    if API_KEY:
        client_key = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(client_key, API_KEY):
            log.warning(f"Rejected connection — invalid or missing API key from {request.headers.get('Host', 'unknown')}")
            return connection.respond(HTTPStatus.FORBIDDEN, "Invalid or missing API key")

    # ── Batch status endpoint ──
    if request.path.startswith("/batch/status/"):
        job_id = request.path[len("/batch/status/"):]
        job = _batch_jobs.get(job_id)
        if not job:
            return _json_response(connection, HTTPStatus.NOT_FOUND,
                                  {"error": "Job not found", "job_id": job_id})

        result = {
            "job_id": job.job_id,
            "status": job.status.value,
            "language": job.language,
            "duration": job.audio_duration,
            "created_at": job.created_at,
        }
        if job.status == JobStatus.COMPLETED:
            result["transcript"] = job.transcript
            result["latency_ms"] = job.latency_ms
            result["completed_at"] = job.completed_at
        elif job.status == JobStatus.FAILED:
            result["error_message"] = job.error_message
            result["completed_at"] = job.completed_at

        return _json_response(connection, HTTPStatus.OK, result)

    # ── Batch result endpoint ──
    if request.path.startswith("/batch/result/"):
        job_id = request.path[len("/batch/result/"):]
        job = _batch_jobs.get(job_id)
        if not job:
            return _json_response(connection, HTTPStatus.NOT_FOUND,
                                  {"error": "Job not found", "job_id": job_id})

        if job.status == JobStatus.COMPLETED:
            return _json_response(connection, HTTPStatus.OK, {
                "job_id": job.job_id,
                "status": "completed",
                "transcript": job.transcript,
                "language": job.language,
                "duration": job.audio_duration,
                "latency_ms": job.latency_ms,
            })
        elif job.status == JobStatus.FAILED:
            return _json_response(connection, HTTPStatus.OK, {
                "job_id": job.job_id,
                "status": "failed",
                "error_message": job.error_message,
            })
        else:
            # Still processing — 202 Accepted
            return _json_response(connection, HTTPStatus.ACCEPTED, {
                "job_id": job.job_id,
                "status": job.status.value,
                "language": job.language,
                "duration": job.audio_duration,
            })

    # ── Fix headers mangled by reverse proxies (e.g. Cloudflare Tunnel) ──
    # Cloudflare rewrites "Connection: Upgrade" → "Connection: keep-alive".
    # If Sec-WebSocket-Key is present, this is a genuine WebSocket client,
    # so restore the expected headers before websockets validates them.
    if request.headers.get("Sec-WebSocket-Key"):
        conn_values = [v.lower() for v in request.headers.get_all("Connection")]
        if not any("upgrade" in v for v in conn_values):
            log.info(f"Fixing Connection header mangled by reverse proxy (was: {request.headers.get('Connection')})")
            del request.headers["Connection"]
            request.headers["Connection"] = "Upgrade"

        upgrade_values = [v.lower() for v in request.headers.get_all("Upgrade")]
        if not any("websocket" in v for v in upgrade_values):
            log.info(f"Fixing Upgrade header mangled by reverse proxy (was: {request.headers.get('Upgrade')})")
            if "Upgrade" in request.headers:
                del request.headers["Upgrade"]
            request.headers["Upgrade"] = "websocket"

    return None


# ─── Main ──────────────────────────────────────────────────────────────────────
async def main():
    global _conn_semaphore, _server_start_time, _batch_queue
    global _batch_worker_task, _batch_cleanup_task

    load_model()

    log.info("Running warm-up inference...")
    dummy = np.zeros(16000, dtype=np.float32)
    _run_inference(dummy, "hi")
    log.info("Warm-up complete")

    _conn_semaphore = asyncio.Semaphore(MAX_CONNECTIONS)
    _server_start_time = time.time()

    # Initialize batch processing
    _batch_queue = asyncio.Queue()
    _batch_worker_task = asyncio.create_task(_batch_worker())
    _batch_cleanup_task = asyncio.create_task(_batch_cleanup_loop())

    log.info(f"Starting VEXYL-STT WebSocket server on ws://{HOST}:{PORT}")

    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s, stop_event))

    async with websockets.serve(
        _limited_handler,
        HOST,
        PORT,
        max_size=10 * 1024 * 1024,   # 10MB max message
        ping_interval=30,
        ping_timeout=10,
        close_timeout=5,
        process_request=_process_request,
        create_connection=BatchCapableConnection,
    ) as server:
        log.info(f"VEXYL-STT server ready | ws://{HOST}:{PORT} | max_conn={MAX_CONNECTIONS} | batch=enabled")
        await stop_event.wait()

        log.info("Shutting down... cancelling batch tasks")
        _batch_worker_task.cancel()
        _batch_cleanup_task.cancel()
        try:
            await _batch_worker_task
        except asyncio.CancelledError:
            pass
        try:
            await _batch_cleanup_task
        except asyncio.CancelledError:
            pass

        log.info("Closing active connections")
        server.close()
        await server.wait_closed()
        log.info("Server stopped cleanly")


def _handle_signal(sig, stop_event: asyncio.Event):
    log.info(f"Received {signal.Signals(sig).name}, initiating shutdown...")
    stop_event.set()


if __name__ == "__main__":
    asyncio.run(main())
