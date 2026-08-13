# 🔌 Connecter ton agent à N'IMPORTE QUEL appareil

Ton agent a maintenant une **passerelle universelle** : une adresse web sécurisée que
Siri, ton navigateur, n8n, ou n'importe quelle app peut appeler pour lui parler.

**Ton adresse (remplace par la tienne si différente) :**
```
https://agent-ia-n8z2.onrender.com/agent/ask
```

---

## ÉTAPE 0 — Activer la passerelle (obligatoire, 2 min)

1. Va sur **[dashboard.render.com](https://dashboard.render.com)** → ton service **agent-ia**.
2. Onglet **Environment** → **Add Environment Variable** :
   - **Key** : `AGENT_API_KEY`
   - **Value** : un mot de passe long, **lettres + chiffres uniquement** (ex : `Lohan2026Secret9xK`)
3. **Save Changes** → Render **redéploie tout seul** (~2 min).

### Tester que ça marche
Dans ton navigateur, colle (remplace `TA_CLE`) :
```
https://agent-ia-n8z2.onrender.com/agent/ask?q=bonjour&key=TA_CLE
```
✅ Tu dois voir `{"answer": "..."}`. Si `501` → clé pas encore posée/redeploy en cours. Si `401` → clé fausse.

---

## 📱 SIRI — parler à l'agent à la voix (iPhone/iPad/Mac/Watch)

1. Ouvre l'app **Raccourcis** → **+** (nouveau raccourci).
2. Ajoute l'action **« Dicter un texte »**.
3. Ajoute **« Obtenir le contenu de l'URL »** :
   - URL : `https://agent-ia-n8z2.onrender.com/agent/ask`
   - Déplie **Afficher plus** → **Méthode : POST**
   - **Corps de la requête : JSON**, ajoute 2 champs :
     - `message` → (texte) → choisis la variable **Texte dicté**
     - `key` → (texte) → `TA_CLE`
4. Ajoute **« Obtenir la valeur du dictionnaire »** → Clé : `answer`
5. Ajoute **« Énoncer le texte »** (pour qu'il te réponde à voix haute).
6. Nomme le raccourci **« Mon agent »**.

➡️ Dis **« Dis Siri, Mon agent »**, parle, et l'agent te répond ! Marche sur **tous tes appareils Apple** (même la montre).

*(Android : l'app **HTTP Shortcuts** ou **Tasker** fait pareil avec la même URL.)*

---

## 🌐 n8n — LE connecteur universel (Gmail, Agenda, 400+ apps)

n8n relie ton agent à quasiment tout, **sans coder**.

### Installer n8n (le plus simple : hébergé)
1. Va sur **[n8n.io](https://n8n.io)** → **Get started** → crée un compte (essai gratuit) — OU auto-héberge-le plus tard.
2. Tu arrives sur un éditeur de **workflows** (des blocs qu'on relie).

### Exemple : « Résume mes nouveaux mails et envoie sur Telegram »
1. **Nouveau workflow** → bloc déclencheur **Gmail → On new email** (connecte ton compte Google en 2 clics, autorisation en lecture).
2. Ajoute un bloc **HTTP Request** :
   - Method : **POST**
   - URL : `https://agent-ia-n8z2.onrender.com/agent/ask`
   - Body : **JSON** →
     ```json
     { "message": "Résume ce mail et dis si c'est urgent : {{ $json.snippet }}", "key": "TA_CLE" }
     ```
3. Ajoute un bloc **Telegram → Send message** → texte = `{{ $json.answer }}` (la réponse de l'agent).
4. **Active** le workflow. → Chaque nouveau mail est résumé par ton agent et poussé sur Telegram. 🎉

> Le même principe marche avec **Google Agenda, Notion, WhatsApp, Slack, un capteur, un bouton…** :
> tu mets ton agent au milieu (HTTP Request → /agent/ask) et n8n s'occupe des connexions.

---

## 🧠 Idées piquées à Hermes Agent (à faire ensuite)
- **Tâches planifiées autonomes** (« résume mes mails à 7h », « bilan de ma journée à 20h ») → via n8n **Schedule Trigger** → /agent/ask.
- **Multi-plateforme** : la passerelle EST déjà ça — Telegram + web + Siri + n8n partagent la même mémoire (profil).
- **Mémoire persistante** : déjà branchée (Supabase).

---

## 🔒 Sécurité
- Sans `AGENT_API_KEY` → passerelle **désactivée** (répond 501, aucune fuite).
- Mauvaise clé → **401**. Garde ta clé secrète (comme un mot de passe).
- Ne mets JAMAIS ta clé dans un endroit public (capture d'écran, repo…).
