'use strict';

/**
 * vexyl-stt-client.js
 * VEXYL-STT Client
 * -------------------------------------------------------
 * Connects to the VEXYL-STT server (vexyl_stt_server.py) over WebSocket.
 * Follows the same interface pattern as groq-stt.js / deepgram-stt.js.
 *
 * Supported languages (all 22 Indian official languages):
 *   ml-IN (Malayalam), hi-IN (Hindi), ta-IN (Tamil), te-IN (Telugu),
 *   kn-IN (Kannada), bn-IN (Bengali), gu-IN (Gujarati), mr-IN (Marathi),
 *   pa-IN (Punjabi), or-IN (Odia), as-IN (Assamese), ur-IN (Urdu) + more
 */

const WebSocket = require('ws');

const VEXYL_STT_URL      = process.env.VEXYL_STT_URL || 'ws://127.0.0.1:8091';
const VEXYL_STT_API_KEY  = process.env.VEXYL_STT_API_KEY || '';
const RECONNECT_DELAY_MS       = 2000;
const MAX_RECONNECT_TRIES      = 3;
const CONNECTION_TIMEOUT       = 10000;  // 10s to connect

class VexylSTT {
    /**
     * @param {string} languageCode  - Language code e.g. 'ml-IN'
     */
    constructor(languageCode) {
        this.languageCode   = languageCode || 'ml-IN';
        this.ws             = null;
        this.sessionId      = `ic_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
        this.isConnected    = false;
        this.isSessionActive = false;
        this.reconnectTries = 0;

        // Callbacks — set by the caller (server.js / audio-session.js)
        this.onTranscript   = null;   // (text: string) => void   — final transcript
        this.onPartial      = null;   // (text: string) => void   — partial (future)
        this.onError        = null;   // (err: Error)  => void
        this.onReady        = null;   // ()            => void    — server confirmed ready

        // Metrics
        this._metrics = {
            totalCalls:    0,
            totalLatencyMs: 0,
            errors:        0
        };
    }

    // ── Public API ────────────────────────────────────────────────────────────

    /**
     * Connect to the VEXYL-STT server and start a session.
     * Resolves when the server sends {"type":"started"}.
     * @returns {Promise<void>}
     */
    async connect() {
        for (let attempt = 1; attempt <= MAX_RECONNECT_TRIES; attempt++) {
            try {
                await this._connectOnce();
                return; // success
            } catch (err) {
                if (attempt < MAX_RECONNECT_TRIES) {
                    console.warn(`[VexylSTT] Connect attempt ${attempt}/${MAX_RECONNECT_TRIES} failed: ${err.message} — retrying in ${RECONNECT_DELAY_MS}ms`);
                    await new Promise(r => setTimeout(r, RECONNECT_DELAY_MS));
                } else {
                    throw err;
                }
            }
        }
    }

    /** @private Single connection attempt — used by connect() retry loop. */
    _connectOnce() {
        return new Promise((resolve, reject) => {
            let settled = false;
            const settle = (fn, val) => {
                if (settled) return;
                settled = true;
                clearTimeout(timeout);
                fn(val);
            };

            const timeout = setTimeout(() => {
                settle(reject, new Error(`VexylSTT: connection timeout after ${CONNECTION_TIMEOUT}ms`));
                this._closeWs();
            }, CONNECTION_TIMEOUT);

            try {
                const wsOptions = {};
                if (VEXYL_STT_API_KEY) {
                    wsOptions.headers = { 'X-API-Key': VEXYL_STT_API_KEY };
                }
                this.ws = new WebSocket(VEXYL_STT_URL, wsOptions);
            } catch (err) {
                settle(reject, new Error(`VexylSTT: failed to create WebSocket — ${err.message}`));
                return;
            }

            this.ws.on('open', () => {
                console.log(`[VexylSTT] Connected to server | session=${this.sessionId} lang=${this.languageCode}`);
            });

            this.ws.on('message', (raw) => {
                let msg;
                try {
                    msg = JSON.parse(raw.toString());
                } catch {
                    return;
                }

                // Validate message shape
                if (!msg || typeof msg.type !== 'string') return;

                switch (msg.type) {

                    case 'ready':
                        // Server is up, send start command
                        this.ws.send(JSON.stringify({
                            type:       'start',
                            lang:       this.languageCode,
                            session_id: this.sessionId
                        }));
                        if (this.onReady) this.onReady();
                        break;

                    case 'started':
                        this.isConnected     = true;
                        this.isSessionActive = true;
                        this.reconnectTries  = 0;
                        console.log(`[VexylSTT] Session started | ${this.sessionId} (${this.languageCode})`);
                        settle(resolve);
                        break;

                    case 'final':
                        this._metrics.totalCalls++;
                        if (typeof msg.latency_ms === 'number' && isFinite(msg.latency_ms)) {
                            this._metrics.totalLatencyMs += msg.latency_ms;
                        }

                        console.log(`[VexylSTT] Transcript: "${msg.text}" | ${msg.latency_ms}ms | ${msg.duration}s audio`);

                        if (msg.text && this.onTranscript) {
                            this.onTranscript(msg.text);
                        }
                        break;

                    case 'stopped':
                        this.isSessionActive = false;
                        break;

                    case 'error':
                        console.error(`[VexylSTT] Server error: ${msg.message}`);
                        this._metrics.errors++;
                        if (this.onError) this.onError(new Error(msg.message));
                        break;

                    case 'pong':
                        break;
                }
            });

            this.ws.on('error', (err) => {
                console.error(`[VexylSTT] WebSocket error: ${err.message}`);
                this._metrics.errors++;
                if (this.onError) this.onError(err);
                settle(reject, err);
            });

            this.ws.on('close', (code, reason) => {
                this.isConnected     = false;
                this.isSessionActive = false;
                console.log(`[VexylSTT] WebSocket closed (${code}): ${reason}`);
            });
        });
    }

    /**
     * Send a chunk of 16kHz 16-bit mono PCM audio to the server.
     * @param {Buffer} pcmBuffer  - Raw PCM bytes (16kHz, 16-bit, mono)
     */
    sendAudio(pcmBuffer) {
        if (!this.isConnected || !this.isSessionActive) {
            return;
        }
        if (!Buffer.isBuffer(pcmBuffer) || pcmBuffer.length === 0) {
            return;
        }

        try {
            this.ws.send(pcmBuffer);
        } catch (err) {
            console.error(`[VexylSTT] sendAudio error: ${err.message}`);
            this._metrics.errors++;
        }
    }

    /**
     * Stop the current session — flushes any buffered audio, then closes.
     * @returns {Promise<void>}
     */
    async stop() {
        if (!this.ws) return;

        return new Promise((resolve) => {
            let resolved = false;
            const done = () => {
                if (resolved) return;
                resolved = true;
                clearTimeout(stopTimeout);
                if (this.ws) this.ws.off('message', stopHandler);
                this._closeWs();
                resolve();
            };

            const stopTimeout = setTimeout(done, 3000);

            const stopHandler = (raw) => {
                try {
                    const msg = JSON.parse(raw.toString());
                    if (msg.type === 'final' && msg.text && this.onTranscript) {
                        this.onTranscript(msg.text);
                    }
                    if (msg.type === 'stopped') {
                        done();
                    }
                } catch { /* ignore */ }
            };

            if (this.isConnected && this.isSessionActive) {
                this.ws.on('message', stopHandler);
                try {
                    this.ws.send(JSON.stringify({ type: 'stop' }));
                } catch {
                    done();
                }
            } else {
                done();
            }
        });
    }

    // ── Metrics ───────────────────────────────────────────────────────────────

    getMetrics() {
        const { totalCalls, totalLatencyMs, errors } = this._metrics;
        return {
            provider:       'vexyl-stt',
            language:       this.languageCode,
            totalCalls,
            errors,
            avgLatencyMs:   totalCalls > 0 ? Math.round(totalLatencyMs / totalCalls) : 0
        };
    }

    // ── Private ───────────────────────────────────────────────────────────────

    _closeWs() {
        if (this.ws) {
            try { this.ws.close(); } catch { /* ignore */ }
            this.ws = null;
        }
        this.isConnected     = false;
        this.isSessionActive = false;
    }
}

// ── Health check ──────────────────────────────────────────────────────────────

/**
 * Quick connectivity test — used by stt-provider.js testAllSTTProviders()
 * @returns {Promise<boolean>}
 */
async function testVexylSTT() {
    return new Promise((resolve) => {
        const wsOptions = {};
        if (VEXYL_STT_API_KEY) {
            wsOptions.headers = { 'X-API-Key': VEXYL_STT_API_KEY };
        }
        const ws = new WebSocket(VEXYL_STT_URL, wsOptions);
        const timeout = setTimeout(() => {
            ws.terminate();
            resolve(false);
        }, 5000);

        ws.on('message', (raw) => {
            try {
                const msg = JSON.parse(raw.toString());
                if (msg.type === 'ready') {
                    clearTimeout(timeout);
                    ws.close();
                    resolve(true);
                }
            } catch { /* ignore */ }
        });

        ws.on('error', () => {
            clearTimeout(timeout);
            resolve(false);
        });
    });
}

function isVexylSTTConfigured() {
    // Self-hosted server — always "configured". Actual reachability
    // is tested by testVexylSTT().
    return true;
}

module.exports = {
    VexylSTT,
    testVexylSTT,
    isVexylSTTConfigured
};
