#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap MasterAgent-Gros sur une VM Oracle Cloud Always Free (Ubuntu 22.04+).
# Usage :
#   git clone <repo> agent-ia && cd agent-ia
#   cp .env.example .env && nano .env      # remplis GROQ_API_KEY, TELEGRAM_TOKEN, etc.
#   bash deploy/setup_oracle.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

echo "🚀 Setup MasterAgent-Gros (Oracle / Docker)"

# 1. Vérifie le .env
if [ ! -f .env ]; then
  echo "⚠️  Aucun .env trouvé. Copie du modèle…"
  cp .env.example .env
  echo "👉 Édite .env (nano .env) avec au minimum GROQ_API_KEY et TELEGRAM_TOKEN, puis relance ce script."
  exit 1
fi

if ! grep -qE '^GROQ_API_KEY=.+' .env && ! grep -qE '^CEREBRAS_API_KEY=.+' .env; then
  echo "⚠️  Ni GROQ_API_KEY ni CEREBRAS_API_KEY renseignés dans .env."
  echo "   L'agent ne pourra pas répondre. Édite .env puis relance."
  exit 1
fi

# 2. Installe Docker + plugin compose si absents
if ! command -v docker >/dev/null 2>&1; then
  echo "📦 Installation de Docker…"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
  echo "ℹ️  Ajouté au groupe docker. Si 'permission denied' plus bas, déconnecte/reconnecte-toi (ou: newgrp docker)."
fi

# compose v2 (plugin) — vérifie
if ! docker compose version >/dev/null 2>&1; then
  echo "📦 Installation du plugin docker compose…"
  sudo apt-get update -y
  sudo apt-get install -y docker-compose-plugin
fi

# 3. Build + run en arrière-plan (redémarre tout seul au reboot grâce à restart: unless-stopped)
echo "🔨 Build de l'image (peut prendre quelques minutes la 1re fois)…"
sudo docker compose up -d --build

echo ""
echo "✅ Agent démarré en 24/7."
echo "   • Logs        : sudo docker compose logs -f"
echo "   • Statut      : sudo docker compose ps"
echo "   • Redémarrer  : sudo docker compose restart"
echo "   • Arrêter     : sudo docker compose down"
echo ""
echo "📱 Envoie /start à ton bot Telegram pour activer les alertes PEA."
echo "🖥️  Pour l'UI web à distance (optionnel) : ssh -L 7860:localhost:7860 <user>@<ip-vm>  puis http://localhost:7860"
