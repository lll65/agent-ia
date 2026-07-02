# 🧪 PROTOCOLE DE TEST — pas à pas, zéro connaissance requise

Suis les étapes **dans l'ordre**. À chaque étape il y a :
- 👉 **CE QUE TU FAIS** (exactement quoi taper/cliquer)
- ✅ **CE QUE TU DOIS VOIR** (pour savoir que ça a marché)

> ⚠️ Règle d'or : **ne saute aucune étape**. Si une étape ne donne pas le ✅ attendu,
> ARRÊTE-toi là et regarde la section « 🆘 Si ça bloque » en bas.

---

# ÉTAPE 0 — Faire tourner l'agent sur ton PC (obligatoire avant tout)

Rien ne marche tant que ça, ça ne marche pas. On fait ça une seule fois.

### 0.1 — Vérifier que Python est installé
👉 Ouvre le **Terminal** :
- **Windows** : touche `Windows`, tape `cmd`, Entrée.
- **Mac** : `Cmd+Espace`, tape `terminal`, Entrée.

👉 Tape :
```
python --version
```
✅ Tu dois voir `Python 3.11.x` (ou `3.10.x`).
❌ Si « python n'est pas reconnu » ou version 3.12+ → va installer Python 3.11 :
https://www.python.org/downloads/release/python-3119/
(Windows : **COCHE la case « Add Python to PATH »** pendant l'install.)

### 0.2 — Aller dans le dossier du code
👉 Décompresse le fichier `agent-ia.zip` quelque part de simple, ex : `C:\agent-ia`.
👉 Dans le terminal, tape (adapte le chemin) :
```
cd C:\agent-ia
```
✅ La ligne du terminal se termine maintenant par `agent-ia>`.

### 0.3 — Créer l'environnement isolé (venv)
👉 Tape ces 2 lignes, une par une :
```
python -m venv venv
```
```
venv\Scripts\activate
```
*(Mac/Linux : `python3 -m venv venv` puis `source venv/bin/activate`)*

✅ Ta ligne commence maintenant par `(venv)`. **C'est important.**

> 📌 À RETENIR : à chaque fois que tu ouvres un NOUVEAU terminal, tu dois refaire
> `cd C:\agent-ia` puis `venv\Scripts\activate` avant de lancer quoi que ce soit.

### 0.4 — Installer les dépendances (une seule fois, ~5 min)
👉 Tape :
```
pip install -r requirements.txt
```
✅ Ça défile pendant quelques minutes puis se termine sans ligne rouge « ERROR ».

### 0.5 — Créer ton fichier de configuration `.env`
👉 Tape :
```
copy .env.example .env
```
*(Mac/Linux : `cp .env.example .env`)*

👉 Ouvre-le pour l'éditer :
```
notepad .env
```
*(Mac : `open -e .env`)*

👉 Trouve la ligne `GROQ_API_KEY=` et colle ta clé Groq juste après le `=` :
```
GROQ_API_KEY=gsk_taCleIci
```
👉 Clé gratuite ici : https://console.groq.com/keys (crée un compte, « Create API Key », copie).
👉 **Enregistre** (Ctrl+S) et ferme le Bloc-notes.

✅ Ton `.env` contient maintenant ta clé Groq.

### 0.6 — Lancer l'agent
👉 Tape :
```
python main.py
```
✅ Après quelques secondes tu dois voir une ligne du genre :
`Interface Gradio: http://localhost:7860`

👉 Ouvre ton navigateur sur **http://localhost:7860**
✅ L'interface de l'agent s'affiche. **Bravo, la base tourne.** 🎉

👉 Pour arrêter : reviens au terminal, `Ctrl+C`.

---

# ÉTAPE 1 — Tester le PEA WATCHER (alertes automatiques Telegram)

But : ton agent te **prévient tout seul** sur ton téléphone.

### 1.1 — Créer un bot Telegram (2 min)
👉 Sur ton téléphone, ouvre **Telegram**.
👉 Cherche **@BotFather** (celui avec la coche bleue), ouvre-le.
👉 Envoie : `/newbot`
👉 Il demande un nom → écris ce que tu veux (ex: `Mon Agent PEA`).
👉 Il demande un username → doit finir par `bot` (ex: `mon_agent_pea_bot`).
✅ Il te répond avec un **token** genre `7123456789:AAExxxxxxxxxxxxxxxx`. **Copie-le.**

### 1.2 — Mettre le token dans `.env`
👉 Sur ton PC : `notepad .env`
👉 Trouve `TELEGRAM_TOKEN=` et colle ton token après le `=` :
```
TELEGRAM_TOKEN=7123456789:AAExxxxxxxxxxxxxxxx
```
👉 Pour le **test**, active le watcher et rends-le bavard (on remettra normal après).
Trouve et modifie ces lignes :
```
WATCHER_ENABLED=true
WATCHER_INTERVAL=300
WATCHER_MOVE_PCT=0.5
```
👉 Enregistre (Ctrl+S), ferme.

### 1.3 — Choisir les valeurs à surveiller
👉 Ouvre le fichier `data\watchlist.txt` :
```
notepad data\watchlist.txt
```
👉 Mets tes valeurs, **une par ligne** (nom ou ticker), par exemple :
```
Valneva
Thales
Amundi Nasdaq PEA
```
👉 Enregistre, ferme.

### 1.4 — Test rapide SANS Telegram (pour voir que ça lit les données)
👉 Dans le terminal (avec `(venv)` actif) :
```
python -m agent.pea_watcher
```
✅ Tu vois les valeurs analysées et d'éventuelles alertes s'afficher dans le terminal.
(C'est un test « à sec » : ça n'envoie rien, ça montre juste ce que ça détecte.)

### 1.5 — Test RÉEL avec push Telegram
👉 Lance l'agent complet :
```
python main.py
```
✅ Dans le terminal tu dois voir `Bot Telegram démarré` et `PEA Watcher démarré`.

👉 Sur Telegram, ouvre **TON bot** (celui que tu viens de créer) et envoie : `/start`
✅ Le bot répond « MasterAgent-Gros connecté ! … Ce chat recevra les alertes ».

👉 Envoie maintenant : `/watch`
✅ Le bot te répond avec l'état de tes valeurs (« X alertes » ou « aucun seuil franchi »).
**→ Ça prouve que la chaîne données → analyse → Telegram fonctionne.** ✅

👉 Attends **5 minutes** sans rien faire.
✅ Tu reçois **tout seul** un message d'alerte 🔔 (grâce à `WATCHER_MOVE_PCT=0.5`, presque
tout mouvement déclenche). **→ Ça prouve l'autonomie : il t'a prévenu sans que tu demandes.** 🎉

### 1.6 — Remettre les réglages normaux
👉 Arrête l'agent (`Ctrl+C`), ouvre `.env`, remets :
```
WATCHER_INTERVAL=1800
WATCHER_MOVE_PCT=5.0
```
👉 Enregistre. (Sinon tu seras spammé d'alertes pour rien.)

**✅ OPTION 1 VALIDÉE** si tu as reçu l'alerte automatique.

---

# ÉTAPE 2 — Tester la MÉMOIRE PERSISTANTE (Supabase)

But : l'agent se souvient de tout, même après redémarrage.

> Note : sur ton PC, la mémoire persiste DÉJÀ (fichier local). Ce test sert à vérifier
> que la version « cloud » marche, celle qui te servira pour le 24/7.

### 2.1 — Créer un projet Supabase (gratuit)
👉 Va sur https://supabase.com → **Start your project** → connecte-toi (GitHub ou email).
👉 Clique **New project**.
👉 Donne un nom, et surtout **choisis un mot de passe de base de données** → **NOTE-LE**.
👉 Clique **Create new project**, attends ~1 minute (barre de chargement).

### 2.2 — Récupérer l'adresse de connexion
👉 En bas à gauche : icône ⚙️ **Project Settings** → **Database**.
👉 Section **Connection string** → onglet **URI**.
👉 Copie la ligne. Elle ressemble à :
```
postgresql://postgres:[YOUR-PASSWORD]@db.abcdefgh.supabase.co:5432/postgres
```
👉 **Remplace `[YOUR-PASSWORD]`** par le mot de passe noté à l'étape 2.1.

### 2.3 — Mettre l'URL dans `.env` + installer le driver
👉 `notepad .env`, trouve `SUPABASE_DB_URL=` et colle ton URL complète :
```
SUPABASE_DB_URL=postgresql://postgres:MonMotDePasse@db.abcdefgh.supabase.co:5432/postgres
```
👉 Enregistre, ferme.
👉 Dans le terminal :
```
pip install psycopg2-binary
```
✅ Se termine sans erreur.

### 2.4 — Vérifier que Supabase est bien pris en compte
👉 Lance :
```
python main.py
```
✅ Dans les logs tu dois voir **exactement** cette ligne :
`Mémoire: backend Supabase (persistant, pgvector).`
❌ Si tu vois `backend local (SQLite + ChromaDB)` → l'URL est mauvaise (voir 🆘 en bas).

### 2.5 — La PREUVE que c'est stocké dans le cloud
👉 Laisse l'agent tourner. Ouvre http://localhost:7860, onglet **Chat**, écris un message
(ex : « je m'appelle Lohan et j'investis sur Valneva »). Envoie.
👉 Retourne sur le site **Supabase** → icône **Table Editor** (à gauche) → table **agent_memory**.
✅ Tu vois **ta phrase apparaître comme une ligne** dans la base cloud.
**→ Ça prouve que la mémoire vit hors de ton PC.** 🎉

**✅ OPTION 2 VALIDÉE** si tu vois tes messages dans la table Supabase.

---

# ÉTAPE 3 — Tester le DÉPLOIEMENT 24/7 (Oracle Cloud)

But : l'agent tourne tout seul en permanence, **même PC éteint**.
⏱️ Compte ~30-45 min la première fois. C'est l'étape la plus longue.

### 3.1 — Créer un compte Oracle Cloud
👉 Va sur https://www.oracle.com/cloud/free/ → **Start for free**.
👉 Remplis. Une **carte bancaire** est demandée pour vérification → **elle n'est PAS débitée**.
👉 Choisis une région proche (ex : **Paris** ou **Frankfurt**). Valide.

### 3.2 — Créer la machine virtuelle (VM)
👉 Dans la console Oracle : menu ☰ → **Compute** → **Instances** → **Create instance**.
👉 Clique **Edit** à côté de « Image and shape » :
   - **Shape** → onglet **Ampere** → `VM.Standard.A1.Flex` → règle **2 OCPUs** et **12 GB**.
   - **Image** → **Canonical Ubuntu 22.04**.
👉 Section **Add SSH keys** → **Save private key** (télécharge le fichier `.key`, garde-le précieusement).
👉 Clique **Create**. Attends que le statut passe au **vert (Running)**.
👉 **Note l'adresse « Public IP address »** affichée.

### 3.3 — Se connecter à la VM (SSH)
👉 Sur ton PC, ouvre un terminal dans le dossier où est ta clé `.key`.
👉 Tape (remplace le nom de la clé et l'IP) :
```
ssh -i ssh-key.key ubuntu@TON.IP.PUBLIQUE
```
*(Windows : si `ssh` inconnu, installe « OpenSSH Client » dans Paramètres → Applications
→ Fonctionnalités facultatives. Mac/Linux : ça marche direct.)*
👉 Il demande « Are you sure… » → tape `yes`.
✅ Tu es connecté : la ligne devient `ubuntu@...:~$`. **Tu es DANS le serveur.**

### 3.4 — Installer le code sur la VM
👉 Tape ces lignes une par une (dans le serveur) :
```
sudo apt update && sudo apt install -y git
```
```
git clone URL_DE_TON_DEPOT agent-ia
```
*(remplace `URL_DE_TON_DEPOT` par le lien de ton repo GitHub)*
```
cd agent-ia
```
```
cp .env.example .env
```
```
nano .env
```
👉 Dans l'éditeur `nano` : remplis **GROQ_API_KEY**, **TELEGRAM_TOKEN**, **SUPABASE_DB_URL**
(la même qu'à l'étape 2 !), et mets **WATCHER_ENABLED=true**.
👉 Pour enregistrer dans nano : `Ctrl+O` puis `Entrée`, puis `Ctrl+X` pour sortir.

> 💡 Utilise **Supabase** ici (étape 2) : comme ça, si un jour la VM est recréée, la
> mémoire de l'agent est sauvegardée dans le cloud.

### 3.5 — Lancer en 24/7
👉 Tape :
```
bash deploy/setup_oracle.sh
```
✅ Le script installe Docker, construit l'image (quelques minutes), et démarre l'agent.
Tu vois à la fin `✅ Agent démarré en 24/7.`

👉 Vérifie les logs :
```
sudo docker compose logs -f
```
✅ Tu dois voir `Bot Telegram démarré` et `PEA Watcher démarré`.
*(Pour quitter l'affichage des logs : `Ctrl+C` — ça n'arrête PAS l'agent, juste l'affichage.)*

### 3.6 — La PREUVE que c'est du 24/7
👉 Sur Telegram, envoie `/start` puis `/watch` à ton bot → il répond.
👉 Maintenant **FERME complètement le terminal SSH** (croix de la fenêtre) et **éteins ton PC**.
👉 Attends, puis rallume ton PC/téléphone et envoie `/watch` à ton bot.
✅ Le bot répond **alors que ton PC était éteint**. **→ C'est officiellement autonome 24/7.** 🎉🎉

**✅ OPTION 3 VALIDÉE** si le bot répond PC éteint.

---

# 🆘 Si ça bloque (les erreurs les plus courantes)

| Ce que tu vois | Ce que ça veut dire / la solution |
|---|---|
| `python n'est pas reconnu` | Python pas installé ou pas dans le PATH. Réinstalle Python 3.11 en cochant « Add to PATH ». |
| La ligne ne commence pas par `(venv)` | Tu as oublié `venv\Scripts\activate`. Refais-le. |
| `pip install` ligne rouge ERROR | Vérifie que tu es bien en Python 3.11 (`python --version`). Refais `pip install --upgrade pip` puis réessaie. |
| `❌ LLM indisponible` | Clé Groq absente ou fausse dans `.env`. Recopie-la depuis console.groq.com. |
| Le bot Telegram ne répond pas | L'agent (`python main.py`) doit être en train de tourner. Vérifie le token dans `.env`. |
| Watcher : aucune alerte | As-tu fait `/start` au bot ? `WATCHER_ENABLED=true` ? `data\watchlist.txt` rempli ? |
| Mémoire : `backend local` au lieu de Supabase | URL Supabase mauvaise, ou `pip install psycopg2-binary` pas fait. Revérifie l'URL (mot de passe remplacé ?). |
| SSH : `Permission denied` | Mauvais fichier de clé, ou mauvais utilisateur. Sur Ubuntu Oracle c'est `ubuntu@...`. |
| Docker : `permission denied` | Déconnecte/reconnecte ta session SSH (ferme et rouvre le SSH), puis relance le script. |

---

# 📌 Résumé en 1 phrase par option

1. **Watcher** : crée un bot Telegram → mets le token dans `.env` → `WATCHER_ENABLED=true`
   → `python main.py` → `/start` puis `/watch` sur Telegram.
2. **Mémoire** : crée un projet Supabase → colle l'URL dans `.env` → `pip install psycopg2-binary`
   → relance → vérifie « backend Supabase » dans les logs.
3. **24/7** : crée une VM Oracle gratuite → SSH → `git clone` + `.env` → `bash deploy/setup_oracle.sh`
   → teste `/watch` PC éteint.

Fais-les **dans l'ordre**, un à la fois. Bon courage ! 💪
