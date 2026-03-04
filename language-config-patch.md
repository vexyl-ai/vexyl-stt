# language-config.js Patch
# =========================
# Add 'vexyl-stt' as a fallback STT provider for all Indian languages.
# This makes VEXYL-STT available when Sarvam is down, or as the primary
# for clients requiring full data sovereignty (no external API calls).

# ──────────────────────────────────────────────────────────────────────────────
# CHANGE — Update each Indian language entry to include 'vexyl-stt'
# ──────────────────────────────────────────────────────────────────────────────

# FIND (ml-IN entry, already exists):
    'ml-IN': {
        name: 'Malayalam',
        nativeName: 'മലയാളം',
        sttProviders: ['sarvam'],
        preferredSTT: 'sarvam',
        ...
    }

# REPLACE with:
    'ml-IN': {
        name: 'Malayalam',
        nativeName: 'മലയാളം',
        sttProviders: ['sarvam', 'vexyl-stt'],
        preferredSTT: 'sarvam',           # Keep Sarvam as default (streaming, battle-tested)
        fallbackSTT:  'vexyl-stt',        # Use VEXYL-STT if Sarvam fails
        ...
    }

# Apply the same pattern for all Indian language entries:
#   hi-IN, ta-IN, te-IN, kn-IN, bn-IN, gu-IN, mr-IN, pa-IN, or-IN, as-IN, ur-IN

# ──────────────────────────────────────────────────────────────────────────────
# OPTIONAL — For full data sovereignty deployments, set as primary:
# ──────────────────────────────────────────────────────────────────────────────
# Set STT_PROVIDER=vexyl-stt in .env
# OR change preferredSTT: 'vexyl-stt' for specific languages
# This routes ALL Indian language calls through the local model, zero API cost.
