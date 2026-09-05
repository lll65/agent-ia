#!/usr/bin/env python3
"""
NOVA PILOTE — le programme qui tourne SUR TON PC et bouge le curseur.

Nova, elle, est sur un serveur à l'autre bout d'Internet : elle n'a aucun accès à ton
écran. C'est CE programme, que tu lances toi-même, qui ouvre un navigateur et clique.
Tant qu'il n'est pas lancé, Nova ne peut rien piloter du tout. C'est voulu : le seul
interrupteur qui compte est entre tes mains.

  INSTALLATION (une fois)
      pip install playwright requests
      playwright install chromium

  LANCEMENT
      python pilote/nova_pilote.py --cle TA_CLE_NOVA --serveur https://ton-app.onrender.com

  ARRÊT
      Ctrl+C dans la fenêtre, ou ferme le navigateur. À tout moment.

⚠️ CE QU'IL NE FERA JAMAIS, ET POURQUOI C'EST ÉCRIT ICI AUSSI
Les mêmes règles existent côté serveur (agent/pilote.py). Elles sont rejouées ici, sur
ta machine, parce qu'une vérification qui n'existe que sur le serveur disparaît si le
serveur se trompe. Ici, c'est ton ordinateur qui refuse — et ça ne dépend de personne.

  • Un navigateur DÉDIÉ, profil vierge, jamais ton Chrome habituel. Sans tes cookies,
    ce navigateur n'est connecté à rien : ni à ta banque, ni à tes mails, ni à tes
    réseaux. C'est la protection la plus solide de toutes, parce qu'elle ne dépend
    d'aucun contrôle : il n'y a simplement rien à atteindre.
  • Banque, paiement, administration : interdits.
  • Mot de passe, code, numéro de carte : jamais tapés.
  • Chaque geste est affiché AVANT d'être fait, avec un temps de lecture, pour que tu
    voies ce qui se passe au lieu de le découvrir après.
"""
import argparse
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Il manque « requests ». Fais : pip install requests")

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Il manque Playwright. Fais :\n"
             "    pip install playwright\n"
             "    playwright install chromium")

# Les règles de sûreté, copiées du serveur. On les redéfinit ici plutôt que de les
# importer : ce fichier doit pouvoir tourner seul sur son PC, sans le reste du projet.
_INTERDITS = (
    "banque", "bank", "credit-agricole", "creditagricole", "bnpparibas", "societegenerale",
    "caisse-epargne", "caisseepargne", "labanquepostale", "boursorama-banque", "fortuneo",
    "hellobank", "revolut", "n26", "lydia", "bourso", "trade-republic", "traderepublic",
    "degiro", "saxobank", "interactivebrokers", "binance", "coinbase", "kraken",
    "paypal", "stripe.com", "checkout", "paiement", "payment", "3dsecure", "systempay",
    "paylib", "klarna", "alma.eu",
    "impots.gouv", "ameli.fr", "franceconnect", "service-public.fr", "urssaf",
    "ants.gouv", "caf.fr", "pole-emploi", "francetravail",
)


# Une page de paiement vit le plus souvent dans le CHEMIN, pas dans le domaine :
# « boutique.fr/commande/payer » doit être refusé comme « paypal.com ».
_CHEMINS = ("/paiement", "/payment", "/checkout", "/3dsecure", "/virement",
            "/transfer", "/pay/", "/carte-bancaire", "/cb-", "/order/pay",
            "/commande/payer", "/souscription", "/mandat")


def interdit(url: str) -> str:
    u = (url or "").lower()
    for motif in _INTERDITS:
        if motif in u:
            return motif
    for motif in _CHEMINS:
        if motif in u:
            return motif.strip("/")
    return ""


def dis(texte: str) -> None:
    """Ce qui s'affiche dans ta fenêtre. C'est ton seul moyen de suivre en direct."""
    print(f"  {texte}", flush=True)


class Pilote:
    def __init__(self, page, lent: float):
        self.page = page
        self.lent = lent          # temps de lecture entre deux gestes

    def joue(self, geste: dict) -> dict:
        quoi = geste.get("quoi")
        cible = geste.get("cible") or ""
        valeur = geste.get("valeur") or ""
        pourquoi = geste.get("pourquoi") or ""

        # ⚠️ On ANNONCE avant de faire, pas après. « Ma souris bouge toute seule » n'est
        # rassurant que si on sait ce qu'elle va faire une seconde avant de le voir.
        dis(f"→ {quoi} {cible[:70]}" + (f"  ({pourquoi[:50]})" if pourquoi else ""))
        time.sleep(self.lent)

        if quoi == "ouvrir":
            motif = interdit(cible)
            if motif:
                return {"ok": False, "raison": f"site interdit au pilote ({motif})"}
            self.page.goto(cible, wait_until="domcontentloaded", timeout=30000)
            return {"ok": True, "url": self.page.url, "titre": self.page.title()}

        # Toute action sur une page : on revérifie où on est. Un site peut rediriger.
        motif = interdit(self.page.url)
        if motif:
            return {"ok": False, "raison": f"la page a redirigé vers un site interdit ({motif})"}

        if quoi == "clic":
            for essai in (self.page.get_by_role("button", name=cible),
                          self.page.get_by_role("link", name=cible),
                          self.page.get_by_text(cible, exact=False)):
                try:
                    essai.first.click(timeout=4000)
                    return {"ok": True, "url": self.page.url}
                except Exception:
                    continue
            return {"ok": False, "raison": f"rien de cliquable nommé « {cible} »"}

        if quoi == "ecrire":
            try:
                champ = (self.page.get_by_label(cible) if cible
                         else self.page.get_by_role("textbox"))
                champ.first.fill(valeur, timeout=4000)
                # Entrée : c'est ce qu'on attend d'une recherche.
                champ.first.press("Enter")
                return {"ok": True, "url": self.page.url}
            except Exception as e:
                return {"ok": False, "raison": f"champ introuvable ({type(e).__name__})"}

        if quoi == "defiler":
            self.page.mouse.wheel(0, 800)
            return {"ok": True}

        if quoi == "attendre":
            try:
                time.sleep(min(10.0, float(cible or 2)))
            except ValueError:
                time.sleep(2)
            return {"ok": True}

        if quoi == "lire":
            texte = self.page.inner_text("body")[:4000]
            return {"ok": True, "texte": texte, "url": self.page.url}

        if quoi == "capture":
            self.page.screenshot(path="pilote_capture.png")
            return {"ok": True, "fichier": "pilote_capture.png"}

        return {"ok": False, "raison": f"geste inconnu « {quoi} »"}


def boucle(serveur: str, cle: str, lent: float) -> None:
    base = serveur.rstrip("/")
    dis("Navigateur dédié en cours d'ouverture (profil vierge : "
        "il n'est connecté à AUCUN de tes comptes)…")
    with sync_playwright() as p:
        # headless=False : tu DOIS le voir. C'est tout l'intérêt.
        nav = p.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = nav.new_context(no_viewport=True, locale="fr-FR")
        page = ctx.new_page()
        page.goto("about:blank")
        pilote = Pilote(page, lent)
        dis("Prêt. J'attends que Nova me donne quelque chose à faire.")
        dis("Ctrl+C pour tout arrêter, à tout moment.\n")

        while True:
            try:
                r = requests.get(f"{base}/agent/pilote/prochain",
                                 params={"key": cle}, timeout=20)
                lot = r.json() if r.status_code == 200 else {}
            except KeyboardInterrupt:
                raise
            except Exception as e:
                dis(f"(serveur injoignable : {type(e).__name__}) — je réessaie")
                time.sleep(5)
                continue

            gestes = (lot or {}).get("gestes") or []
            if not gestes:
                time.sleep(2)
                continue

            print(f"\n▶ Nova me demande : « {lot.get('demande', '')} »", flush=True)
            resultats = []
            for g in gestes:
                try:
                    res = pilote.joue(g)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    res = {"ok": False, "raison": f"{type(e).__name__}: {str(e)[:120]}"}
                resultats.append({**g, **res})
                if not res.get("ok"):
                    # ⚠️ On s'arrête au premier échec. Continuer un plan dont une étape a
                    # raté, c'est agir sur une page qui n'est pas celle qu'on croyait.
                    dis(f"✗ {res.get('raison')} — j'arrête là, je ne devine pas la suite.")
                    break
                dis("✓")
            try:
                requests.post(f"{base}/agent/pilote/resultat",
                              params={"key": cle},
                              json={"id": lot.get("id"), "etapes": resultats},
                              timeout=20)
            except Exception:
                dis("(je n'ai pas pu prévenir Nova du résultat)")
            print("— terminé, j'attends la suite.\n", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Nova pilote un navigateur dédié sur ce PC.")
    ap.add_argument("--serveur", required=True, help="https://ton-app.onrender.com")
    ap.add_argument("--cle", required=True, help="ta clé Nova")
    ap.add_argument("--lent", type=float, default=1.2,
                    help="secondes entre deux gestes, pour que tu puisses suivre")
    a = ap.parse_args()
    print("\n" + "=" * 62)
    print("  NOVA PILOTE — navigateur dédié")
    print("  Tant que cette fenêtre est ouverte, Nova peut piloter CE navigateur.")
    print("  Ferme-la et elle ne peut plus rien.")
    print("=" * 62 + "\n")
    try:
        boucle(a.serveur, a.cle, a.lent)
    except KeyboardInterrupt:
        print("\nArrêté. Nova n'a plus la main.\n")


if __name__ == "__main__":
    main()
