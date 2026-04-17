#!/bin/bash
set -e

echo "=========================================="
echo "  Installation Agent IA Personnel"
echo "=========================================="

# 1. Python deps
echo ""
echo "[1/3] Installation des dépendances Python..."
pip install -r requirements.txt

# 2. Ollama
echo ""
echo "[2/3] Installation d'Ollama (LLM local gratuit)..."
if ! command -v ollama &> /dev/null; then
    curl -fsSL https://ollama.com/install.sh | sh
    echo "Ollama installé."
else
    echo "Ollama déjà installé."
fi

# 3. Téléchargement du modèle
echo ""
echo "[3/3] Téléchargement du modèle LLM (Llama3 ~4GB, une seule fois)..."
echo "Démarre Ollama en arrière-plan..."
ollama serve &
OLLAMA_PID=$!
sleep 3
ollama pull llama3
kill $OLLAMA_PID 2>/dev/null || true

echo ""
echo "=========================================="
echo "  Installation terminée !"
echo "=========================================="
echo ""
echo "Pour démarrer ton agent IA:"
echo "  1. ollama serve          (dans un terminal)"
echo "  2. python main.py        (dans un autre terminal)"
echo "  3. Ouvre: http://localhost:8000/docs"
echo ""
