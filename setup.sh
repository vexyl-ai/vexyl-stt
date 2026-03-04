#!/bin/bash
# ============================================================
# VEXYL-STT — One-step Setup Script
# ============================================================
# Sets up Python venv, installs dependencies, authenticates
# with HuggingFace, downloads the model, and creates .env
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

MODEL_ID="ai4bharat/indic-conformer-600m-multilingual"
VENV_DIR="venv"
ENV_FILE=".env"

# ── Colors ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

step=0
total_steps=6

print_step() {
    step=$((step + 1))
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  [$step/$total_steps] $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

print_ok() {
    echo -e "  ${GREEN}✓${NC} $1"
}

print_warn() {
    echo -e "  ${YELLOW}!${NC} $1"
}

print_error() {
    echo -e "  ${RED}✗${NC} $1"
}

# ── Header ──────────────────────────────────────────────────
echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║${NC}  ${BOLD}VEXYL-STT Server — Setup${NC}                            ${CYAN}║${NC}"
echo -e "${CYAN}║${NC}  ai4bharat/indic-conformer-600m-multilingual         ${CYAN}║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════╝${NC}"

# ── Step 1: Check Python 3 ─────────────────────────────────
print_step "Checking Python 3"

if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 --version 2>&1)
    print_ok "Found: $PY_VERSION"
else
    print_warn "Python 3 not found. Attempting install via Homebrew..."
    if command -v brew &>/dev/null; then
        brew install python3
        PY_VERSION=$(python3 --version 2>&1)
        print_ok "Installed: $PY_VERSION"
    else
        print_error "Python 3 is required. Please install it:"
        echo "         macOS:  brew install python3"
        echo "         Ubuntu: sudo apt install python3 python3-venv"
        exit 1
    fi
fi

# ── Step 2: Create virtual environment ─────────────────────
print_step "Creating Python virtual environment"

if [ -d "$VENV_DIR" ]; then
    print_ok "Virtual environment already exists at ./$VENV_DIR"
else
    python3 -m venv "$VENV_DIR"
    print_ok "Created virtual environment at ./$VENV_DIR"
fi

# Activate
source "$VENV_DIR/bin/activate"
print_ok "Activated venv ($(python3 --version))"

# Upgrade pip quietly
pip install --upgrade pip -q
print_ok "pip upgraded"

# ── Step 3: Install dependencies ───────────────────────────
print_step "Installing Python dependencies"

echo -e "  ${CYAN}Installing PyTorch (CPU)...${NC}"
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu -q
print_ok "torch + torchaudio (CPU)"

echo -e "  ${CYAN}Installing transformers, websockets, numpy, onnxruntime...${NC}"
pip install transformers websockets numpy onnxruntime -q
print_ok "transformers, websockets, numpy, onnxruntime"

# ── Step 4: HuggingFace authentication ─────────────────────
print_step "HuggingFace authentication"

# Check if already logged in
LOGGED_IN=false
if python3 -c "from huggingface_hub import HfApi; HfApi().whoami()" &>/dev/null; then
    HF_USER=$(python3 -c "from huggingface_hub import HfApi; print(HfApi().whoami()['name'])")
    print_ok "Already logged in as: $HF_USER"
    LOGGED_IN=true
fi

if [ "$LOGGED_IN" = false ]; then
    echo ""
    echo -e "  ${YELLOW}This model is gated and requires a HuggingFace token.${NC}"
    echo ""
    echo -e "  ${BOLD}Before proceeding, make sure you have:${NC}"
    echo "    1. Created an account at https://huggingface.co"
    echo "    2. Requested access at:"
    echo "       https://huggingface.co/$MODEL_ID"
    echo "    3. Created a token at:"
    echo "       https://huggingface.co/settings/tokens"
    echo ""
    read -rp "  Enter your HuggingFace token (hf_...): " HF_TOKEN

    if [ -z "$HF_TOKEN" ]; then
        print_error "No token provided. Exiting."
        exit 1
    fi

    python3 -c "from huggingface_hub import login; login(token='$HF_TOKEN')" 2>/dev/null
    if python3 -c "from huggingface_hub import HfApi; HfApi().whoami()" &>/dev/null; then
        HF_USER=$(python3 -c "from huggingface_hub import HfApi; print(HfApi().whoami()['name'])")
        print_ok "Logged in as: $HF_USER"
    else
        print_error "Login failed. Please check your token and try again."
        exit 1
    fi
fi

# ── Step 5: Download model ─────────────────────────────────
print_step "Downloading IndicConformer model"

# Check if model is already cached
if python3 -c "
from transformers import AutoModel
AutoModel.from_pretrained('$MODEL_ID', trust_remote_code=True, local_files_only=True)
" &>/dev/null 2>&1; then
    CACHE_SIZE=$(du -sh ~/.cache/huggingface/hub/models--ai4bharat--indic-conformer-600m-multilingual 2>/dev/null | cut -f1)
    print_ok "Model already cached ($CACHE_SIZE)"
else
    echo -e "  ${CYAN}Downloading $MODEL_ID (~2.4 GB)...${NC}"
    echo -e "  ${CYAN}This may take a few minutes depending on your connection.${NC}"
    echo ""

    python3 -c "
from transformers import AutoModel
model = AutoModel.from_pretrained('$MODEL_ID', trust_remote_code=True)
print()
"
    if [ $? -eq 0 ]; then
        CACHE_SIZE=$(du -sh ~/.cache/huggingface/hub/models--ai4bharat--indic-conformer-600m-multilingual 2>/dev/null | cut -f1)
        print_ok "Model downloaded ($CACHE_SIZE)"
    else
        print_error "Model download failed."
        echo ""
        echo "  Common issues:"
        echo "    - You haven't been granted access yet"
        echo "      Visit: https://huggingface.co/$MODEL_ID"
        echo "    - Your token doesn't have read permissions"
        echo "      Check: https://huggingface.co/settings/tokens"
        exit 1
    fi
fi

# ── Step 6: Create .env and run.sh ─────────────────────────
print_step "Creating config files"

if [ -f "$ENV_FILE" ]; then
    print_ok ".env already exists (keeping existing)"
else
    cat > "$ENV_FILE" <<'EOF'
VEXYL_STT_HOST=127.0.0.1
VEXYL_STT_PORT=8091
VEXYL_STT_DECODE=ctc
VEXYL_STT_DEVICE=cpu
EOF
    print_ok "Created .env"
fi

if [ -f "run.sh" ]; then
    print_ok "run.sh already exists (keeping existing)"
else
    cat > run.sh <<'SCRIPT'
#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi
source venv/bin/activate
python3 vexyl_stt_server.py
SCRIPT
    chmod +x run.sh
    print_ok "Created run.sh"
fi

# ── Done ────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║${NC}  ${BOLD}Setup complete!${NC}                                     ${GREEN}║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}To start the server:${NC}"
echo "    ./run.sh"
echo ""
echo -e "  ${BOLD}To test in browser:${NC}"
echo "    open test.html"
echo ""
echo -e "  ${BOLD}Server will listen on:${NC}"
echo "    ws://127.0.0.1:8091"
echo ""
echo -e "  ${BOLD}Config:${NC}"
echo "    .env          — Server settings"
echo "    run.sh        — Start script"
echo "    test.html     — Browser test client"
echo ""
echo -e "  Model cached at:"
echo "    ~/.cache/huggingface/hub/models--ai4bharat--indic-conformer-600m-multilingual"
echo ""
