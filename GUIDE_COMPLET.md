# 📘 GUIDE COMPLET — MasterAgent-Gros (autonome, sans assistance)

Tout ce qu'il te faut pour : installer sur un PC, débloquer le web, activer la vidéo IA,
et déployer en 24/7. **Copier-coller** les blocs `comme ça` dans la fenêtre noire (terminal).

---

## PARTIE 1 — Installer l'agent sur ce PC (le plus puissant)

### 1.1 Prérequis (à installer une fois)
- **Python 3.11** : https://www.python.org/downloads/release/python-3119/
  → pendant l'install, **COCHE « Add python.exe to PATH »**.
- **Git** : https://git-scm.com/download/win (installe avec Suivant partout).
- **FFmpeg** (pour la vidéo) — après avoir ouvert un terminal :
  ```
  winget install Gyan.FFmpeg
  ```
  Puis **ferme et rouvre** le terminal.

### 1.2 Récupérer le code + installer
Ouvre un terminal dans le dossier où tu veux l'agent (ex: `Documents`) — dans l'explorateur,
barre d'adresse → tape `cmd` → Entrée. Puis, lignes une par une :
```
git clone -b claude/trusting-lamport-zs5wI https://github.com/lll65/agent-ia.git
```
```
cd agent-ia
```
```
python -m venv venv
```
```
venv\Scripts\activate
```
```
pip install -r requirements.txt
```

### 1.3 Configurer tes clés
```
copy .env.example .env
```
```
notepad .env
```
Remplis **au minimum** ces lignes (récupère les valeurs depuis le `.env` de ton autre PC) :
```
GROQ_API_KEY=gsk_ta_cle_groq
CEREBRAS_API_KEY=csk_ta_cle_cerebras     (optionnel, secours si Groq sature)
TELEGRAM_TOKEN=ton_token_telegram
FAL_API_KEY=ta_cle_fal                    (pour la vidéo IA, voir Partie 3)
WATCHER_ENABLED=true
WATCHER_INTERVAL=1800
WATCHER_MOVE_PCT=5.0
```
Enregistre (`Ctrl+S`), ferme.

### 1.4 Lancer
```
python main.py
```
→ ouvre **http://localhost:7860**. Envoie **/start** à ton bot Telegram.

> 💡 **Astuce** : double-clique **`démarrer.bat`** pour lancer sans taper de commande.

### 1.5 Ce PC est plus puissant ? (GPU)
Si ce PC a une **carte graphique ≥ 12 Go de VRAM**, tu peux faire tourner **en local et gratuit** :
- La **vidéo IA illimitée** (serveur `video/svd_server.py`)
- Un **LLM 100% local** (Ollama : `ollama pull llama3.1:8b`, puis vider les clés cloud dans `.env`)

Vérifie ta VRAM : `Ctrl+Maj+Échap` → onglet Performance → GPU → "Mémoire GPU dédiée".
(Avec 8 Go : reste sur Groq + fal.ai. Avec 12 Go+ : le local devient intéressant.)

---

## PARTIE 2 — Débloquer le web (antivirus RAV)

Ton **RAV Endpoint Protection** bloque la connexion streaming (raisonnement en direct + Conseiller Pro).
Le chat marche déjà (grâce au correctif `queue=False`), ceci débloque juste le **live**.

### Option simple — ajouter une exception (recommandé)
1. Ouvre **RAV Endpoint Protection**.
2. Va dans **Settings / Paramètres** → **Web Protection** (ou "Protection Web" / "Safe Browsing").
3. Cherche **Exclusions / Exceptions / Liste blanche**.
4. Ajoute : **`localhost`** et **`127.0.0.1`**, OU ajoute le programme
   `C:\...\agent-ia\venv\Scripts\python.exe` en exclusion.
5. Enregistre, relance l'agent, teste.

### Option test rapide (pour confirmer que c'est bien lui)
Désactive temporairement la **Protection Web** de RAV (5 min) → recharge la page → écris "salut".
- Ça marche en direct → c'est bien RAV → remets la protection ET ajoute l'exception ci-dessus.
- Ça ne change rien → ce n'est pas lui, garde la protection (le chat marche déjà de toute façon).

> ⚠️ Ne **supprime pas** RAV complètement. Une exception `localhost` suffit et garde ton PC protégé.
> (RAV est parfois envahissant — si tu ne t'en sers pas, tu peux le désinstaller et garder juste
> **Windows Security**, qui lui ne bloque pas localhost. Panneau de config → Programmes → Désinstaller.)

---

## PARTIE 3 — Vidéo IA (fal.ai)

### 3.1 Obtenir la clé
1. Va sur **https://fal.ai** → inscris-toi (gratuit, ~5$ de crédits offerts = ~100-200 vidéos).
2. **Dashboard → Keys → Add key** → copie la clé.

### 3.2 La mettre dans l'agent
```
notepad .env
```
→ ligne `FAL_API_KEY=ta_cle`, enregistre. Relance `démarrer.bat`.

### 3.3 Générer une vraie vidéo IA
Onglet **« 🖼️→🎬 Réaliste »** → **charge une image** → **Générer** → l'IA anime ton image en vraie vidéo.

> **Quand les crédits fal.ai seront épuisés**, pour de l'illimité gratuit :
> - **Ton GPU en local** (si ≥ 12 Go) : lance `python video/svd_server.py` sur ce PC.
> - **Google Colab** (GPU gratuit) : héberge le modèle sur Colab. (Setup plus long.)

---

## PARTIE 4 — Déploiement 24/7 (Oracle Cloud, gratuit à vie)

Pour que l'agent surveille ton PEA **PC éteint**. Détail complet aussi dans `DEPLOIEMENT.md`.

### 4.1 Créer le compte + la machine
1. https://www.oracle.com/cloud/free/ → **Start for free** (carte bancaire pour vérif, **non débitée**).
2. Région proche (Paris/Frankfurt).
3. Menu ☰ → **Compute → Instances → Create instance**.
4. **Edit** (Image and shape) → Shape : onglet **Ampere** → `VM.Standard.A1.Flex` → **2 OCPU / 12 GB**.
5. Image : **Canonical Ubuntu 22.04**.
6. **Add SSH keys** → **Save private key** (télécharge le fichier `.key`, garde-le !).
7. **Create**. Attends le statut **vert**. Note l'**IP publique**.

### 4.2 Se connecter (depuis ton PC)
```
ssh -i chemin\vers\ta-cle.key ubuntu@TON.IP.PUBLIQUE
```
(tape `yes` à la question). Tu es dans le serveur : la ligne devient `ubuntu@...:~$`.

### 4.3 Installer l'agent sur le serveur
```
sudo apt update && sudo apt install -y git
```
```
git clone -b claude/trusting-lamport-zs5wI https://github.com/lll65/agent-ia.git
```
```
cd agent-ia
```
```
cp .env.example .env
```
```
nano .env
```
Remplis `GROQ_API_KEY`, `TELEGRAM_TOKEN`, `WATCHER_ENABLED=true` (et `SUPABASE_DB_URL` si tu fais l'étape C).
Enregistrer dans nano : **`Ctrl+O`** → Entrée → **`Ctrl+X`**.

### 4.4 Lancer en 24/7
```
bash deploy/setup_oracle.sh
```
Ça installe Docker, construit l'image et démarre l'agent (redémarrage auto, survit aux reboots).

Vérifier :
```
sudo docker compose logs -f
```
Tu dois voir `Bot Telegram démarré` + `PEA Watcher démarré`. (Quitter l'affichage : `Ctrl+C`, ça n'arrête pas l'agent.)

### 4.5 Confirmer
Envoie **`/start`** puis **`/watch`** à ton bot. **Éteins ton PC.** Réenvoie `/watch` → il répond = **24/7 confirmé.** 🎉

### Commandes serveur utiles
```
sudo docker compose ps         # état
sudo docker compose restart    # redémarrer
sudo docker compose down       # arrêter
cd agent-ia && git pull && sudo docker compose up -d --build   # mettre à jour
```

---

## AIDE-MÉMOIRE (quotidien sur PC)

| Je veux… | Je fais… |
|---|---|
| Lancer l'agent | Double-clic **`démarrer.bat`** |
| Ouvrir l'interface | http://localhost:7860 |
| Voir le raisonnement | Bouton **Mode Agent** (ou préfixe `agent:`) |
| Modifier mes valeurs surveillées | Éditer `data\watchlist.txt` |
| Récupérer mes dernières corrections | `git pull` (dans le dossier, `(venv)` actif) |
| Réinstaller les libs après un `git pull` | `pip install -r requirements.txt` |
| Alertes PEA | Automatiques sur Telegram (+ `/watch` à la demande) |

**Clés gratuites :** Groq → console.groq.com/keys · Cerebras → cloud.cerebras.ai · fal.ai → fal.ai/dashboard/keys · Telegram → @BotFather

Bon courage — tu as tout ce qu'il faut pour être autonome. 💪
