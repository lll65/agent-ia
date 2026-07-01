# 🚀 Déploiement de l'Agent IA sur ton PC (GPU 8 Go)

Guide pas-à-pas pour faire tourner **MasterAgent-Gros** en local sur ta machine.

---

## 📋 Ce dont tu as besoin

| Élément | Détail |
|---|---|
| **OS** | Windows 10/11, Linux ou macOS |
| **Python** | 3.10 ou 3.11 (⚠️ **pas** 3.12+, certaines libs ne suivent pas encore) |
| **RAM système** | 8 Go minimum, 16 Go conseillé |
| **GPU** | 8 Go VRAM (ta carte) — utile pour le LLM local + la vidéo IA |
| **Disque** | ~10 Go libres (dont ~5 Go pour le modèle LLM local) |
| **Connexion** | Internet (pour Groq/Cerebras + recherche web + données bourse) |

> 💡 **Important :** le GPU n'est **PAS obligatoire** pour faire tourner l'agent.
> Avec une clé Groq/Cerebras, tout le "cerveau" tourne dans le cloud (gratuit).
> Le GPU 8 Go sert seulement si tu veux : (A) un LLM **100 % local** via Ollama,
> ou (B) la **génération vidéo IA** (Stable Video Diffusion).

---

## ⚡ Option 1 — Déploiement RAPIDE (cloud, sans GPU) — recommandé pour démarrer

L'agent utilise Groq (gratuit) comme cerveau. Zéro téléchargement de modèle.

### Étape 1 — Installer Python 3.11
- **Windows** : télécharge sur [python.org/downloads](https://www.python.org/downloads/release/python-3119/)
  → coche **« Add Python to PATH »** pendant l'installation.
- **Linux (Ubuntu/Debian)** : `sudo apt install python3.11 python3.11-venv python3-pip`
- **macOS** : `brew install python@3.11`

Vérifie :
```bash
python --version      # doit afficher 3.11.x  (ou python3 --version)
```

### Étape 2 — Récupérer le code
Décompresse le ZIP `agent-ia.zip` dans un dossier, par exemple `C:\agent-ia`.
Ouvre un terminal **dans ce dossier** :
- **Windows** : dans l'explorateur, tape `cmd` dans la barre d'adresse du dossier.
- **Linux/macOS** : `cd /chemin/vers/agent-ia`

### Étape 3 — Créer un environnement virtuel (isole les dépendances)
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux / macOS
python3 -m venv venv
source venv/bin/activate
```
Ton invite doit maintenant commencer par `(venv)`.

### Étape 4 — Installer les dépendances
```bash
pip install --upgrade pip
pip install -r requirements.txt
```
⏳ 3–5 minutes (télécharge gradio, chromadb, yfinance, etc.).

### Étape 5 — Configurer les clés API
Copie le modèle de configuration :
```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```
Ouvre `.env` avec un éditeur de texte (Bloc-notes, VS Code…) et remplis **au moins** :
```
GROQ_API_KEY=gsk_ta_cle_ici
```
👉 Clé Groq **gratuite** : [console.groq.com/keys](https://console.groq.com/keys)

**Recommandé** — ajoute le fallback Cerebras (au cas où Groq sature) :
```
CEREBRAS_API_KEY=csk_ta_cle_ici
```
👉 Clé Cerebras **gratuite** : [cloud.cerebras.ai](https://cloud.cerebras.ai)

### Étape 6 — Lancer l'agent 🎉
```bash
python main.py
```
Attends le message `Interface Gradio: http://localhost:7860`, puis ouvre
ton navigateur sur **http://localhost:7860**.

Pour arrêter : `Ctrl + C` dans le terminal.

---

## 🖥️ Option 2 — LLM 100 % LOCAL sur ton GPU 8 Go (via Ollama)

Si tu veux que le cerveau tourne **sur ta carte graphique** (aucune clé cloud,
100 % privé, fonctionne hors-ligne).

### Étape A — Installer Ollama
Télécharge et installe depuis [ollama.com/download](https://ollama.com/download).
(Ollama détecte automatiquement ton GPU NVIDIA/AMD.)

### Étape B — Télécharger un modèle adapté à 8 Go de VRAM
```bash
ollama pull llama3.1:8b
```
Ce modèle (quantifié Q4) occupe **~5–6 Go de VRAM** → parfait pour ta carte.

> Alternatives si 8B rame :
> - `ollama pull llama3.2:3b` (plus léger, ~3 Go, plus rapide)
> - `ollama pull qwen2.5:7b` (bon en raisonnement)
> - `ollama pull mistral:7b`

### Étape C — Configurer `.env` pour Ollama
**Vide** les clés cloud pour forcer le local (l'ordre de priorité choisit Groq
en premier si sa clé est présente) :
```
GROQ_API_KEY=
XAI_API_KEY=
CEREBRAS_API_KEY=
GEMINI_API_KEY=
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
```

### Étape D — Lancer
Assure-toi qu'Ollama tourne (icône dans la barre des tâches, ou `ollama serve`),
puis :
```bash
python main.py
```

> ⚠️ **Perf attendue sur 8 Go VRAM :** ~15–40 tokens/seconde avec llama3.1:8b.
> C'est plus lent que Groq (qui est ultra-rapide) mais **gratuit et privé**.
> Astuce : garde Groq comme cerveau principal et n'utilise Ollama que si tu veux
> l'offline. Tu peux basculer juste en changeant `.env` et en relançant.

---

## 🎬 Option 3 — Activer la génération VIDÉO IA sur ton GPU

Le module vidéo « Réaliste » peut utiliser **Stable Video Diffusion** sur ton GPU.

Sur 8 Go de VRAM, SVD complet est **juste** (il lui faut idéalement 10–12 Go).
Deux choix :
1. **Sans rien faire** : l'agent bascule automatiquement sur le mode **Ken Burns**
   (zoom/pan cinématique via FFmpeg) — ça marche sur n'importe quel PC.
2. **Vraie IA** : installe [FFmpeg](https://ffmpeg.org/download.html) et, si tu
   veux SVD, lance le serveur GPU fourni : `python video/svd_server.py`
   (nécessite `pip install torch diffusers` avec CUDA — gourmand).

Pour 8 Go, je recommande le mode Ken Burns (option 1) : aucun réglage, résultat immédiat.

---

## 🔔 PEA Watcher — alertes automatiques sur ton téléphone

L'agent peut **surveiller tes positions en continu** et t'envoyer une alerte
Telegram quand un seuil est franchi (RSI survendu/suracheté, gros mouvement).
Aucune action de ta part : il tourne en tâche de fond.

### Étape 1 — Créer un bot Telegram (2 min, gratuit)
1. Sur Telegram, ouvre **@BotFather** → `/newbot` → choisis un nom.
2. Il te donne un **token** (ex: `123456:ABC-...`). Copie-le dans `.env` :
   ```
   TELEGRAM_TOKEN=123456:ABC-ton-token
   ```

### Étape 2 — Définir ta watchlist
Édite **`data/watchlist.txt`** (une valeur par ligne, nom ou ticker) :
```
Valneva
Thales
Amundi Nasdaq PEA
```
> Si tu laisses ce fichier vide, le watcher surveille automatiquement les valeurs
> de tes portefeuilles sauvegardés dans l'interface web.

### Étape 3 — Activer le watcher
Dans `.env` :
```
WATCHER_ENABLED=true
WATCHER_INTERVAL=1800     # scan toutes les 30 min
WATCHER_RSI_LOW=32        # alerte si RSI ≤ 32 (survendu)
WATCHER_RSI_HIGH=70       # alerte si RSI ≥ 70 (suracheté)
WATCHER_MOVE_PCT=5.0      # alerte si mouvement journalier ≥ 5 %
```

### Étape 4 — Tester AVANT de déployer
```bash
# Scan unique sans rien envoyer (voir ce qui se déclencherait) :
python -m agent.pea_watcher

# Scan unique + envoi Telegram réel (envoie /start au bot d'abord) :
python -m agent.pea_watcher --send
```

### Étape 5 — Lancer
1. Sur Telegram, envoie **`/start`** à ton bot (il mémorise où pousser).
2. Lance l'agent : `python main.py`.
3. Tu recevras désormais des alertes du type :
   ```
   🔔 Alerte PEA — 01/07 14:30
   🟢 Valneva (VLA.PA) — 3.85€
      RSI 28 ≤ 32 → SURVENDU, zone d'achat potentielle | j: -6.2%
   ```
> 💬 Commande **`/watch`** dans Telegram = scan immédiat à la demande.

---

## 🧠 Mémoire persistante (Supabase) — l'agent ne t'oublie plus

Par défaut, la mémoire est locale (SQLite + ChromaDB). C'est parfait **sur ton PC**.
Mais sur un hébergeur gratuit éphémère (Render free), le disque est effacé à chaque
mise en veille → l'agent oublie tout. Solution : externaliser la mémoire vers
**Supabase** (Postgres + pgvector), gratuit et persistant.

> 💡 Sur ton PC, tu n'as **rien à faire** : sans `SUPABASE_DB_URL`, l'agent utilise
> la mémoire locale qui persiste déjà très bien. Cette étape n'est utile que pour un
> déploiement 24/7 sur un hôte éphémère (voir Oracle Free plus bas).

### Étape 1 — Créer un projet Supabase (gratuit, 2 min)
1. Va sur [supabase.com](https://supabase.com) → **New project**.
2. Choisis un mot de passe de base de données (note-le).
3. Attends ~1 min que la base soit prête.

### Étape 2 — Récupérer l'URL de connexion
Dans le projet : **Project Settings → Database → Connection string → URI**.
Copie l'URL (remplace `[YOUR-PASSWORD]` par ton mot de passe) dans `.env` :
```
SUPABASE_DB_URL=postgresql://postgres:MON_MOT_DE_PASSE@db.xxxx.supabase.co:5432/postgres
```

### Étape 3 — Installer le driver Postgres
```bash
pip install psycopg2-binary
```
(déjà inclus si tu réinstalles `requirements.txt`)

### Étape 4 — C'est tout
Lance `python main.py`. Au démarrage tu verras dans les logs :
```
Mémoire: backend Supabase (persistant, pgvector).
```
L'agent crée automatiquement la table `agent_memory` et l'extension `vector`.
Désormais tout ce que tu lui dis est stocké dans Supabase et **survit à tout redémarrage**.

> - L'extension pgvector et la table sont créées toutes seules au premier lancement.
> - Les embeddings sont générés en local (MiniLM, via chromadb déjà installé) → aucune API payante.
> - Si Supabase est injoignable, l'agent retombe **automatiquement** sur la mémoire locale (aucun crash).

---

## 🔧 Dépannage (problèmes fréquents)

| Symptôme | Cause / Solution |
|---|---|
| `python n'est pas reconnu` | Python pas dans le PATH → réinstalle en cochant « Add to PATH », ou utilise `py` au lieu de `python` (Windows). |
| `pip install` échoue sur une lib | Vérifie Python 3.10/3.11 (pas 3.12+). Mets pip à jour : `pip install --upgrade pip`. |
| Port 7860 déjà utilisé | Change `GRADIO_PORT=7861` dans `.env`. |
| `❌ LLM indisponible` | Clé API manquante/invalide dans `.env`, ou pas de connexion. Vérifie la clé Groq. |
| Erreur 429 en boucle | Groq saturé → ajoute `CEREBRAS_API_KEY` dans `.env` (fallback auto). |
| Ollama : `connection refused` | Ollama pas démarré → lance `ollama serve` ou ouvre l'app Ollama. |
| Données bourse « N/D » | Yahoo Finance limite parfois les requêtes → réessaie dans quelques secondes. |
| Gradio ne s'ouvre pas | Attends le message `http://localhost:7860`, puis ouvre-le manuellement dans le navigateur. |
| Watcher : aucune alerte reçue | Envoie `/start` au bot (il doit connaître ton chat), vérifie `WATCHER_ENABLED=true` et que `data/watchlist.txt` n'est pas vide. Teste avec `python -m agent.pea_watcher --send`. |
| Watcher : « Aucune cible » dans les logs | Définis `TELEGRAM_CHAT_ID` dans `.env`, ou envoie `/start` au bot au moins une fois. |
| Supabase : « fallback local » dans les logs | `psycopg2` non installé (`pip install psycopg2-binary`) ou `SUPABASE_DB_URL` incorrecte. L'agent tourne quand même en mémoire locale. |
| Supabase : timeout de connexion | Vérifie le mot de passe dans l'URL et que ton IP n'est pas bloquée (Supabase autorise tout par défaut). Port 5432 (ou 6543 en mode pooler). |

---

## 📁 Structure du projet (pour t'y retrouver)

```
agent-ia/
├── main.py                 ← point d'entrée (lance API + interface web)
├── config.py               ← configuration (lit le .env)
├── .env                    ← TES clés API (à créer, jamais partagé)
├── requirements.txt        ← dépendances Python
├── agent/                  ← cœur de l'agent (ReAct, finance, auto-amélioration)
│   ├── core.py             ← boucle de raisonnement ReAct
│   ├── finance_deep.py     ← analyse financière (ETF/actions dynamiques)
│   └── system_prompt.py    ← personnalité / directives de l'agent
├── llm/client.py           ← routeur LLM (Groq → Cerebras → Gemini → Ollama)
├── plugins/builtin/        ← outils (finance, fichiers, code, web…)
├── memory/                 ← mémoire vectorielle (ChromaDB)
├── ui/gradio_app.py        ← interface web (onglets Chat, Finance, Vidéo…)
├── video/                  ← génération vidéo
├── data/                   ← base de données, sessions, univers ETF (créé auto)
│   └── pea_etf_universe.json  ← 100+ ETFs PEA pour le screener
└── output/                 ← fichiers générés (vidéos, projets…)
```

---

## ✅ Récap ultra-court

```bash
# 1. Installer Python 3.11, puis dans le dossier du projet :
python -m venv venv && venv\Scripts\activate     # (Linux: source venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env                            # (Linux: cp)
# 2. Mettre GROQ_API_KEY=... dans .env
# 3. Lancer :
python main.py
# 4. Ouvrir http://localhost:7860
```

Bon déploiement ! 🚀
