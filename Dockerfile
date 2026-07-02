FROM python:3.11-slim

WORKDIR /app

# ffmpeg fournit aussi ffprobe (pas de paquet séparé) ; libgomp1 requis par
# onnxruntime (chromadb / embeddings) ; fonts pour la génération de slides.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgomp1 \
    fonts-dejavu \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data output output/videos data/agents data/chroma data/plugins

# API 8000 + UI 7860. Sur un serveur public, ne mappe ces ports que sur 127.0.0.1
# (voir docker-compose.yml) — l'agent n'a besoin d'aucun port entrant : Telegram
# et le LLM cloud fonctionnent uniquement en sortie.
EXPOSE 8000 7860

CMD ["python", "main.py"]
