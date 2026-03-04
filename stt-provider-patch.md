# stt-provider.js Integration Patch
# ===================================
# Apply these 4 changes to your existing stt-provider.js
# Each section shows EXACTLY where to add the new lines.

# ──────────────────────────────────────────────────────────────────────────────
# CHANGE 1 — Add import at the top (after existing requires)
# ──────────────────────────────────────────────────────────────────────────────

# FIND this block (already exists):
const { OpenAISTT, isOpenAISTTConfigured, testOpenAISTT } = require('./openai-stt.js');

# ADD after it:
const { VexylSTT, testVexylSTT, isVexylSTTConfigured } = require('./vexyl-stt-client.js');


# ──────────────────────────────────────────────────────────────────────────────
# CHANGE 2 — Add to initializeSTTProvider() function
# ──────────────────────────────────────────────────────────────────────────────

# FIND this block (already exists):
    if (provider === 'openai') {
        const openaiInstance = new OpenAISTT(languageCode);
        return {
            type: 'openai',
            instance: openaiInstance,
            mode: 'batch',
            requiresSilenceDetection: true
        };
    }

# ADD after it:
    if (provider === 'vexyl-stt') {
        const vexylInstance = new VexylSTT(languageCode);
        return {
            type: 'vexyl-stt',
            instance: vexylInstance,
            mode: 'stream',             // streaming like Sarvam/Deepgram
            requiresSilenceDetection: false  // server has built-in VAD
        };
    }


# ──────────────────────────────────────────────────────────────────────────────
# CHANGE 3 — Add to testAllSTTProviders() function
# ──────────────────────────────────────────────────────────────────────────────

# FIND (already exists):
    const results = {
        sarvam: false,
        groq: false,
        gemini: false,
        deepgram: false,
        openai: false
    };

# REPLACE with:
    const results = {
        sarvam: false,
        groq: false,
        gemini: false,
        deepgram: false,
        openai: false,
        'vexyl-stt': false      // ← add this line
    };

# FIND (near end of testAllSTTProviders, after OpenAI test block):
    return results;

# ADD before it:
    // Test VEXYL-STT server
    if (isVexylSTTConfigured()) {
        try {
            results['vexyl-stt'] = await testVexylSTT();
            console.log(`${results['vexyl-stt'] ? '✅' : '❌'} VexylSTT: ${results['vexyl-stt'] ? 'Server running' : 'Server not reachable'}`);
        } catch (error) {
            console.log(`❌ VexylSTT: Failed - ${error.message}`);
        }
    } else {
        console.log('⚠️  VexylSTT: Not configured');
    }


# ──────────────────────────────────────────────────────────────────────────────
# CHANGE 4 — Add to switchSTTProvider() validation list
# ──────────────────────────────────────────────────────────────────────────────

# FIND (already exists) — the valid providers check:
    const validProviders = ['auto', 'sarvam', 'groq', 'gemini', 'deepgram', 'openai'];

# REPLACE with:
    const validProviders = ['auto', 'sarvam', 'groq', 'gemini', 'deepgram', 'openai', 'vexyl-stt'];
