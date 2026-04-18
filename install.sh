#!/bin/bash
set -e

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RESET="\033[0m"

echo -e "${BOLD}=========================================="
echo -e "  Agent IA Personnel v2.0 — Installation"
echo -e "==========================================${RESET}"

# 1. Ollama
echo -e "\n${BOLD}[1/4] Ollama (LLM local)${RESET}"
if ! command -v ollama &> /dev/null; then
    echo "Installation d'Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
else
    echo "Ollama déjà installé: $(ollama --version)"
fi

# 2. FFmpeg
echo -e "\n${BOLD}[2/4] FFmpeg (traitement vidéo)${RESET}"
if ! command -v ffmpeg &> /dev/null; then
    if command -v apt-get &> /dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg fonts-dejavu
    elif command -v brew &> /dev/null; then
        brew install ffmpeg
    else
        echo "${YELLOW}FFmpeg non trouvé. Installe-le manuellement: https://ffmpeg.org${RESET}"
    fi
else
    echo "FFmpeg déjà installé: $(ffmpeg -version 2>&1 | head -1)"
fi

# 3. Dépendances Python
echo -e "\n${BOLD}[3/4] Dépendances Python${RESET}"
pip install -r requirements.txt -q

# 4. Modèle LLM
echo -e "\n${BOLD}[4/4] Téléchargement du modèle Llama3 (~4GB)${RESET}"
echo "Démarrage d'Ollama..."
ollama serve &>/dev/null &
OLLAMA_PID=$!
sleep 4

MODEL=${OLLAMA_MODEL:-llama3}
echo "Téléchargement de '$MODEL'..."
ollama pull "$MODEL"
kill $OLLAMA_PID 2>/dev/null || true

# .env
if [ ! -f .env ]; then
    cp .env.example .env
    echo -e "\n${YELLOW}⚠️  Fichier .env créé depuis .env.example"
    echo "   Édite-le pour configurer les bots Telegram/Discord.${RESET}"
fi

echo -e "\n${GREEN}${BOLD}=========================================="
echo "  Installation terminée !"
echo -e "==========================================${RESET}"
echo ""
echo "Pour démarrer:"
echo -e "  ${BOLD}Terminal 1:${RESET} ollama serve"
echo -e "  ${BOLD}Terminal 2:${RESET} python main.py"
echo ""
echo "Accès:"
echo -e "  API:  http://localhost:8000/docs"
echo -e "  UI:   http://localhost:7860"
