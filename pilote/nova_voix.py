#!/usr/bin/env python3
r"""
NOVA VOIX — une vraie voix neuronale, sur TON PC, hors ligne, et gratuite pour de bon.

« trouve une tech gratuite où je peux avoir des voix vraiment cool sur mon pc »

CE QUE C'EST. Piper : un moteur de synthèse vocale neuronal qui tourne EN LOCAL, sur
processeur, sans compte, sans clé, sans carte bancaire, et sans envoyer une seule ligne
de ton texte à qui que ce soit. Ce programme le lance et le rend joignable par Nova.

⚠️ AVANT D'INSTALLER QUOI QUE CE SOIT, ESSAIE L'AUTRE PISTE.
Ouvre simplement Nova dans **Microsoft Edge**, puis ⚙️ → 🎙️ Voix de Nova, et choisis
« Microsoft Denise (Natural) ». Edge embarque les voix neuronales de Microsoft, elles
sont excellentes, et ça ne demande AUCUNE installation. Ce fichier n'a d'intérêt que si
tu veux une voix qui ne dépend de personne : pas d'Internet, pas d'Edge, pas d'un
service qui peut fermer.

  INSTALLATION SUR WINDOWS (une fois), à coller telle quelle dans l'invite de commandes

      mkdir C:\nova
      cd C:\nova
      curl -L -o nova_voix.py https://raw.githubusercontent.com/lll65/agent-ia/claude/trusting-lamport-zs5wI/pilote/nova_voix.py
      python -m pip install piper-tts

  LANCEMENT
      cd C:\nova
      python nova_voix.py

⚠️ « 'pip' n'est pas reconnu en tant que commande interne ou externe. » C'est le cas le
plus courant sous Windows : Python est installé, mais son dossier Scripts n'est pas dans
le PATH. Rien à réparer — écris `python -m pip` au lieu de `pip`, ça passe par le Python
que tu viens d'utiliser, donc forcément le bon.

⚠️ « can't open file 'C:\Users\Lohan\pilote\nova_voix.py' ». Ce fichier vit dans le dépôt
GitHub, pas sur ton PC : `python pilote/nova_voix.py` ne peut marcher que depuis une
copie locale du projet. D'où le `curl` ci-dessus — un seul fichier, rien à cloner.

  Au premier lancement, il télécharge la voix (~65 Mo) et la range à côté de lui.
  Ensuite, plus jamais de réseau.

  ARRÊT
      Ctrl+C. Nova revient toute seule à la voix du navigateur.

  CHOISIR UNE AUTRE VOIX
      python pilote/nova_voix.py --voix fr_FR-tom-medium
  Voix françaises connues : fr_FR-siwis-medium (femme, nette),
  fr_FR-tom-medium (homme), fr_FR-upmc-medium (femme), fr_FR-gilles-low (homme, léger).

⚠️ POURQUOI 127.0.0.1 ET PAS « localhost ».
Nova est servie en HTTPS depuis Render. Une page HTTPS n'a pas le droit d'aller chercher
du contenu en HTTP — sauf sur l'adresse de bouclage : Chrome exempte explicitement
http://127.0.0.1 depuis la version 53. « localhost » n'a PAS la même exemption dans tous
les navigateurs. On écrit donc l'adresse en chiffres, et pas parce que c'est plus joli.

⚠️ CE QUE JE N'AI PAS PU VÉRIFIER, ET C'EST BEAUCOUP.
Aucune sortie réseau depuis mon environnement de développement, et aucun navigateur : je
n'ai jamais vu Piper s'installer, ni parler, ni Nova aller le chercher. Le découpage, le
serveur et le repli sont testés ; la chaîne complète, non. Si ça ne marche pas, ce
programme dit quoi et pourquoi au lieu de rester muet — envoie-moi le message.
"""
import argparse
import http.server
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

PORT = 5111
DOSSIER = Path(__file__).resolve().parent / "voix"
BASE_HF = ("https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
           "fr/fr_FR/{parleur}/{qualite}/{nom}")

# Un texte plus long que ça n'est pas une phrase, c'est un document : on refuse plutôt
# que de faire ramer sa machine pendant une minute sans qu'il sache pourquoi.
MAX_TEXTE = 4000


def dis(t: str) -> None:
    print(f"  {t}", flush=True)


def _decoupe_nom(voix: str):
    """« fr_FR-siwis-medium » → (« siwis », « medium »)."""
    bouts = voix.split("-")
    if len(bouts) != 3 or not bouts[1] or not bouts[2]:
        raise SystemExit(f"Nom de voix incompris : « {voix} ».\n"
                         "Attendu : fr_FR-<parleur>-<qualité>, par exemple fr_FR-siwis-medium")
    return bouts[1], bouts[2]


def telecharge(voix: str) -> Path:
    """Le modèle et sa fiche de config, téléchargés une seule fois."""
    parleur, qualite = _decoupe_nom(voix)
    DOSSIER.mkdir(parents=True, exist_ok=True)
    modele = DOSSIER / f"{voix}.onnx"
    fiche = DOSSIER / f"{voix}.onnx.json"
    # ⚠️ Les DEUX fichiers sont nécessaires. Un modèle sans sa fiche de config échoue
    # plus loin, avec un message de Piper qui ne dit pas qu'il manque un fichier.
    for cible, nom in ((modele, f"{voix}.onnx"), (fiche, f"{voix}.onnx.json")):
        if cible.exists() and cible.stat().st_size > 0:
            continue
        url = BASE_HF.format(parleur=parleur, qualite=qualite, nom=nom)
        dis(f"Téléchargement de {nom}… (une seule fois)")
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(cible, "wb") as f:
                shutil.copyfileobj(r, f)
        except Exception as e:
            # Un fichier à moitié écrit ferait échouer TOUS les lancements suivants
            # avec un message incompréhensible. On le retire.
            cible.unlink(missing_ok=True)
            raise SystemExit(
                f"Téléchargement impossible ({type(e).__name__}).\n"
                f"  Adresse : {url}\n"
                "  Vérifie ta connexion, ou télécharge les deux fichiers à la main et "
                f"pose-les dans {DOSSIER}")
    return modele


def _commande_piper():
    """Le programme piper, où qu'il soit. `pip install` ne le met pas toujours au PATH."""
    trouve = shutil.which("piper")
    if trouve:
        return [trouve]
    # Sous Windows, pip installe souvent dans Scripts/ sans que le PATH suive.
    return [sys.executable, "-m", "piper"]


def _piper_installe() -> bool:
    """Piper est-il réellement là ? On le DEMANDE, on ne le suppose pas."""
    if shutil.which("piper"):
        return True
    try:
        r = subprocess.run([sys.executable, "-c", "import piper"],
                           capture_output=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


# ⚠️ DEUX FAÇONS DE DONNER LE TEXTE À PIPER, ET ELLES ONT CHANGÉ.
# L'ancien piper lisait le texte sur l'entrée standard ; piper1-gpl le prend en
# ARGUMENT, après « -- ». Je n'ai pas pu exécuter Piper ici (aucun réseau, aucune
# machine Windows) : plutôt que de parier sur une des deux, on essaie, et on RETIENT
# celle qui a marché. Un pari perdu, ce serait un serveur muet sans explication.
_FORMES = (
    ("argument", lambda mod, out, txt: (["-m", str(mod), "-f", str(out), "--", txt], None)),
    ("stdin",    lambda mod, out, txt: (["-m", str(mod), "-f", str(out)], txt.encode("utf-8"))),
)
_FORME_OK = None


def parle(texte: str, modele: Path) -> bytes:
    """Le WAV, ou une exception dont le message est lisible."""
    global _FORME_OK
    formes = [f for f in _FORMES if _FORME_OK is None or f[0] == _FORME_OK] or list(_FORMES)
    echecs = []
    for nom, construit in formes:
        with tempfile.TemporaryDirectory() as d:
            sortie = Path(d) / "voix.wav"
            args, entree = construit(modele, sortie, texte)
            try:
                r = subprocess.run(_commande_piper() + args, input=entree,
                                   capture_output=True, timeout=120)
            except Exception as e:
                echecs.append(f"{nom}: {type(e).__name__}: {e}")
                continue
            if r.returncode == 0 and sortie.exists() and sortie.stat().st_size > 44:
                if _FORME_OK != nom:
                    _FORME_OK = nom
                    dis(f"(Piper accepte le texte en « {nom} »)")
                return sortie.read_bytes()
            detail = (r.stderr or b"").decode("utf-8", "replace").strip()[:300]
            echecs.append(f"{nom}: code {r.returncode} {detail or ''}".strip())
    # ⚠️ Si la forme mémorisée cesse de marcher, on ne s'entête pas dessus.
    _FORME_OK = None
    raise RuntimeError("piper a échoué — " + " | ".join(echecs[:2]))


class Poste(http.server.BaseHTTPRequestHandler):
    modele = None
    voix = ""

    def log_message(self, *a):
        pass                                    # le journal par défaut noie la fenêtre

    def _repond(self, code, corps, type_mime, entetes=None):
        self.send_response(code)
        self.send_header("Content-Type", type_mime)
        self.send_header("Content-Length", str(len(corps)))
        # ⚠️ Nova est servie depuis un AUTRE domaine (Render). Sans ces en-têtes, le
        # navigateur refuserait la réponse — et sans rien afficher dans la page.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        for k, v in (entetes or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corps)

    def do_OPTIONS(self):
        self._repond(204, b"", "text/plain",
                     {"Access-Control-Allow-Headers": "*",
                      "Access-Control-Allow-Methods": "GET, OPTIONS"})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/etat":
            import json
            self._repond(200, json.dumps({"ok": True, "voix": self.voix}).encode(),
                         "application/json")
            return
        if u.path != "/voix":
            self._repond(404, b"rien ici", "text/plain")
            return
        texte = (q.get("text") or [""])[0].strip()
        if not texte:
            self._repond(400, b"aucun texte", "text/plain")
            return
        if len(texte) > MAX_TEXTE:
            texte = texte[:MAX_TEXTE]
        try:
            wav = parle(texte, self.modele)
        except Exception as e:
            # ⚠️ On répond une ERREUR, pas un silence. Un fichier audio vide, le
            # navigateur le joue sans rien dire : Nova aurait l'air de parler dans le
            # vide, et personne ne saurait pourquoi.
            msg = f"{type(e).__name__}: {e}"
            dis(f"✗ {msg[:200]}")
            self._repond(500, msg.encode("utf-8", "replace")[:900], "text/plain")
            return
        self._repond(200, wav, "audio/wav")


def main() -> None:
    ap = argparse.ArgumentParser(description="Une voix neuronale locale pour Nova.")
    ap.add_argument("--voix", default="fr_FR-siwis-medium",
                    help="fr_FR-siwis-medium (défaut), fr_FR-tom-medium, fr_FR-upmc-medium…")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()

    print("\n" + "=" * 64)
    print("  NOVA VOIX — synthèse neuronale locale (Piper)")
    print("  Rien ne sort de ton ordinateur : le texte n'est envoyé à personne.")
    print("=" * 64 + "\n")

    # ⚠️ Vérifier que Piper est INSTALLÉ avant de télécharger 65 Mo de voix. Sinon on
    # fait patienter pour rien, et l'échec arrive à la fin — au pire moment.
    if not _piper_installe():
        raise SystemExit(
            "\n✗ Piper n'est pas installé.\n\n"
            "  Sous Windows, si « pip » n'est pas reconnu, écris plutôt :\n"
            f"      {Path(sys.executable).name} -m pip install piper-tts\n"
            "  (ça passe par le Python que tu viens d'utiliser, donc forcément le bon)\n\n"
            f"  Ton Python : {sys.version.split()[0]}\n"
            "  ⚠️ Si l'installation échoue en parlant de « onnxruntime » ou de « wheel »,\n"
            "     c'est que ta version de Python est trop récente pour Piper. Installe\n"
            "     Python 3.12 à côté (python.org) et relance avec celui-là — ou reste sur\n"
            "     Microsoft Edge, dont les voix sont excellentes et ne demandent rien.\n")

    modele = telecharge(a.voix)
    dis(f"Voix : {a.voix}")
    dis("Essai à blanc…")
    try:
        wav = parle("Bonjour Lohan.", modele)
    except Exception as e:
        raise SystemExit(
            f"\n✗ Piper n'a pas pu parler : {type(e).__name__}: {e}\n\n"
            "  As-tu bien fait :  pip install piper-tts\n"
            "  Sur Linux il faut parfois aussi :  sudo apt install espeak-ng\n")
    dis(f"✓ Piper répond ({len(wav)} octets de son).")

    Poste.modele, Poste.voix = modele, a.voix
    # ⚠️ 127.0.0.1 et pas 0.0.0.0 : ce serveur ne doit être joignable que depuis CETTE
    # machine. Ouvert sur le réseau, n'importe qui du Wi-Fi pourrait le faire parler.
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), Poste)
    dis(f"Prêt sur http://127.0.0.1:{a.port} — Nova la trouvera toute seule.")
    dis("Laisse cette fenêtre ouverte. Ctrl+C pour revenir à la voix du navigateur.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêté. Nova reprend la voix du navigateur.\n")


if __name__ == "__main__":
    main()
