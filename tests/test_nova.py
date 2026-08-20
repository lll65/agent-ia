"""
Suite de tests Nova — vérifie les chemins critiques SANS réseau ni clés.

But : attraper les régressions avant le déploiement (routage, extraction, gestion d'erreur,
robustesse aux valeurs vides/None). Lancer avec :  python tests/test_nova.py
"""
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# --- Stubs : le test doit tourner sans dépendances lourdes ni réseau ---------
if "tenacity" not in sys.modules:
    t = types.ModuleType("tenacity")

    def _passthrough(*a, **k):
        def deco(f):
            return f
        return deco

    for _n in ("retry", "stop_after_attempt", "wait_exponential",
               "retry_if_exception_type", "before_sleep_log"):
        setattr(t, _n, _passthrough)

    class RetryError(Exception):
        pass

    t.RetryError = RetryError
    sys.modules["tenacity"] = t

import api.agent as A                                    # noqa: E402
from plugins.builtin.visual_maker import make_visual     # noqa: E402

OK, KO = [], []


def check(nom, got, want):
    (OK if got == want else KO).append((nom, got, want))


def check_true(nom, got):
    check(nom, bool(got), True)


# ── 1. Routage des messages ───────────────────────────────────────────────────
def test_routage():
    # Discussion / infos perso : jamais d'outil
    for m in ("salut", "j'ai 17 ans", "je m'appelle Lohan", "merci beaucoup"):
        check_true(f"chat direct: {m}", A._is_smalltalk(m))
    # Vraies demandes : elles doivent partir vers l'agent
    for m in ("mon agenda cette semaine ?", "résume l'actu tech", "crée un ticket linear"):
        check(f"pas du chat: {m}", A._is_smalltalk(m), False)
    # Apps
    check("app agenda", A._detect_toolkit("mon agenda cette semaine"), "googlecalendar")
    check("app maps", A._detect_toolkit("itinéraire pour aller à Lyon"), "googlemaps")
    check("app canva", A._detect_toolkit("crée un design canva"), "canva")
    check("pas une app", A._detect_toolkit("j'ai 17 ans"), None)
    # Finance : masquée sauf demande explicite
    check("finance implicite", A._finance_intent("j'ai 17 ans"), False)
    check("finance explicite", A._finance_intent("parle-moi de l'action Valneva"), True)
    # Visuels vs Canva
    check_true("visuel", A._wants_visual("fais un visuel avec écrit Bienvenue"))
    check("canva simple", A._wants_visual("crée une présentation Canva"), False)
    # Projet GitHub
    check_true("projet github", A._is_github_project("crée un projet github : site portfolio"))
    check("liste github", A._is_github_project("liste mes dépôts github"), False)
    # Questions de capacité
    check_true("capacité", A._is_capability_question("as-tu accès à mon github"))
    check("action", A._is_capability_question("crée un dépôt github"), False)


# ── 2. Détection des échecs (anti-hallucination) ──────────────────────────────
def test_echecs():
    vrai_succes = '✅ [GOOGLECALENDAR_EVENTS_LIST] résultat :\n{"items": [{"summary": "Travail"}]}'
    liste_vide = '✅ [GOOGLECALENDAR_EVENTS_LIST] résultat :\n{"items": []}'
    err_401 = '❌ 401 Invalid API key'
    err_cache = '✅ [CANVA] résultat : {"http_error": "401 Client Error: Unauthorized for url: https://api.canva.com/x"}'
    err_400 = '✅ [CANVA] résultat : {"http_error": "400 Bad Request", "message": "One of design_type must be defined"}'
    check("succès réel", A._looks_like_failure(vrai_succes), False)
    check("liste vide = succès", A._looks_like_failure(liste_vide), False)
    check_true("401 détecté", A._looks_like_failure(err_401))
    check_true("erreur cachée dans un 200", A._looks_like_failure(err_cache))
    check_true("400 détecté", A._looks_like_failure(err_400))
    check_true("400 = corrigeable", A._is_param_error(err_400))
    check("401 = pas corrigeable", A._is_param_error(err_401), False)
    check("vide = échec", A._looks_like_failure(""), True)
    check("None = échec", A._looks_like_failure(None), True)


# ── 3. Dates et agenda ────────────────────────────────────────────────────────
def test_dates():
    import datetime as dt
    lundi = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(days=dt.datetime.now(dt.timezone.utc).weekday())).strftime("%Y-%m-%d")
    tmin, tmax, lib = A._time_bounds("mon agenda cette semaine")
    check("semaine courante", tmin[:10], lundi)
    check("libellé semaine", lib, "cette semaine")
    t2, _, lib2 = A._time_bounds("mon agenda semaine prochaine")
    check_true("semaine prochaine après", t2 > tmin)
    check("libellé prochaine", lib2, "la semaine prochaine")
    _, _, lib3 = A._time_bounds("mon agenda demain")
    check("libellé demain", lib3, "demain")
    # Création vs consultation
    act, args = A._resolve_app_action("mon agenda cette semaine ?")
    check("consultation", act, "GOOGLECALENDAR_EVENTS_LIST")
    act2, args2 = A._resolve_app_action("ajoute un rdv dentiste lundi à 14h")
    check("création", act2, "GOOGLECALENDAR_QUICK_ADD")
    check_true("titre nettoyé", "ajoute" not in (args2 or {}).get("text", "").lower())


# ── 4. Nettoyage des titres ───────────────────────────────────────────────────
def test_titres():
    check("Nova retiré", A._clean_event_text("Nova ajoute à mon agenda une tâche travail demain"),
          "travail demain")
    d = A._canva_design_args("crée une présentation Canva intitulée Projet X")
    check("preset présentation", d["design_type"]["name"], "presentation")
    check("titre canva", d["title"], "Projet X")
    d2 = A._canva_design_args("va sur canva et ecrit salut cest nova")
    check("preset doc", d2["design_type"]["name"], "doc")
    check_true("design_type est un objet", isinstance(d2["design_type"], dict))


# ── 5. Robustesse aux valeurs vides / None ────────────────────────────────────
def test_robustesse():
    for fn, nom in ((A._is_smalltalk, "_is_smalltalk"), (A._detect_toolkit, "_detect_toolkit"),
                    (A._finance_intent, "_finance_intent"), (A._wants_visual, "_wants_visual"),
                    (A._is_capability_question, "_is_capability_question"),
                    (A._is_github_project, "_is_github_project"), (A._is_briefing, "_is_briefing"),
                    (A._is_personal_fact, "_is_personal_fact")):
        for val in ("", "   "):
            try:
                fn(val)
                OK.append((f"{nom}({val!r})", True, True))
            except Exception as e:
                KO.append((f"{nom}({val!r})", f"exception {type(e).__name__}", "pas d'exception"))
    # Schéma vide / bizarre
    check("schéma None", A._trim_schema(None), {})
    check("schéma liste", A._trim_schema([1, 2]), {})
    check_true("schéma imbriqué", "properties" in A._trim_schema(
        {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "string"}}}}}))
    # Découpe TTS
    check("tts vide", A._split_tts(""), [])
    long = "Bonjour. " * 60
    parts = A._split_tts(long)
    check_true("tts découpé", all(len(p) <= 180 for p in parts))
    check_true("tts plafonné", len(parts) <= 12)


# ── 6. Générateur de visuels ──────────────────────────────────────────────────
def test_visuels():
    from PIL import Image
    for fmt, taille in (("carre", (1080, 1080)), ("slide", (1920, 1080)), ("story", (1080, 1920))):
        p = make_visual("Test Nova", "sous-titre", "nova", fmt)
        check(f"visuel {fmt}", Image.open(p).size, taille)
    # Texte très long : ne doit pas planter
    p = make_visual("Un texte vraiment très long " * 8, "", "sombre", "post")
    check_true("texte long", Path(p).exists())
    # Caractères spéciaux / accents dans le nom de fichier
    p2 = make_visual("Éàü ç — @#$%", "", "clair", "carre")
    check_true("caractères spéciaux", Path(p2).exists())


# ── 7. Profil (mémoire structurée) ────────────────────────────────────────────
def test_profil():
    from agent import profile as P
    P._FILE = Path("/tmp/nova_test_profile.json")
    P.clear_all()
    P.add_fact("identite", "A 17 ans")
    P.add_fact("lieu", "Habite à Lyon")
    P.add_fact("identite", "A 17 ans")            # doublon
    facts = P.list_facts()
    check("dédoublonnage", len(facts), 2)
    check_true("bloc de contexte", "17 ans" in P.context_block())
    P.delete_fact(facts[0]["id"])
    check("suppression", len(P.list_facts()), 1)
    P.clear_all()
    check("tout effacé", P.list_facts(), [])
    check("contexte vide", P.context_block(), "")


# ── 8. Automatisations ────────────────────────────────────────────────────────
def test_automatisations():
    from agent import automations as Au
    Au._FILE = Path("/tmp/nova_test_auto.json")
    Au._save([])
    it = Au.add("Test", "Résume mes mails", 18)
    check("créée", it["hour"], 18)
    check_true("active par défaut", it["active"])
    Au.update(it["id"], active=False)
    check("mise en pause", Au.list_all()[0]["active"], False)
    check_true("supprimée", Au.delete(it["id"]))
    check("suppression inconnue", Au.delete("zzz"), False)
    Au._save([])


# ── 9. Escouade ───────────────────────────────────────────────────────────────
def test_escouade():
    from agent import squad as S
    check("agenda", S.pick_agent("mon agenda cette semaine"), "agenda")
    check("mails", S.pick_agent("envoie un mail à Paul"), "mails")
    check("dev", S.pick_agent("crée un dépôt github"), "dev")
    check("aucun", S.pick_agent("salut"), "nova")
    S.record("agenda", "googlecalendar", "TEST")
    snap = S.snapshot()
    check_true("activité enregistrée", snap["total"] >= 1)
    check_true("agent actif", "agenda" in snap["active"])
    check_true("escouade complète", len(snap["squad"]) >= 7)


# ── 10. Caches à expiration + reprise d'identité ──────────────────────────────
def test_caches():
    A.invalidate_caches()
    A._cache_put(A._USERID_CACHE, "canva", "user-A")
    check("cache lu", A._cache_get(A._USERID_CACHE, "canva"), "user-A")
    A.invalidate_caches("canva")
    check("cache vidé", A._cache_get(A._USERID_CACHE, "canva"), None)
    # Expiration
    import time
    A._cache_put(A._USERID_CACHE, "x", "v")
    old = A._CACHE_TTL
    A._CACHE_TTL = -1                       # tout est périmé
    check("expiration", A._cache_get(A._USERID_CACHE, "x"), None)
    A._CACHE_TTL = old
    A.invalidate_caches()

    # Reprise auto quand l'identité en cache est périmée
    appels = []

    def faux(loader, nom, params):
        appels.append(params.get("user_id"))
        return ("❌ 404 No connected account found for user ID old for toolkit canva"
                if len(appels) == 1 else '✅ [CANVA] résultat : {"id":"D1"}')

    import agent.self_heal as SH
    vrai = SH.safe_tool_call
    SH.safe_tool_call = faux
    ids = iter(["old", "nouveau"])
    vrai_uid = A._toolkit_user_id
    A._toolkit_user_id = lambda s: next(ids, "nouveau")
    try:
        out = A._tool_call("CANVA_CREATE", {}, "canva")
        check("reprise après cache périmé", A._looks_like_failure(out), False)
        check("2 tentatives", len(appels), 2)
    finally:
        SH.safe_tool_call = vrai
        A._toolkit_user_id = vrai_uid


# ── 11a. Identifiants d'app : Composio écrit « google_maps », nous « googlemaps » ──
def test_slugs():
    check("normalisation", A._norm_slug("google_maps"), A._norm_slug("googlemaps"))
    check("tirets aussi", A._norm_slug("google-maps"), "googlemaps")
    vrai = A._connected_accounts
    A._connected_accounts = lambda: [("google_maps", "user-42", "ACTIVE")]
    A._USERID_CACHE.clear()
    try:
        check("slug réel", A._real_slug("googlemaps"), "google_maps")
        check("identité retrouvée", A._toolkit_user_id("googlemaps"), "user-42")
    finally:
        A._connected_accounts = vrai
        A._USERID_CACHE.clear()


# ── 11c. Modèles LLM : auto-guérison quand un modèle est retiré ───────────────
def test_modeles():
    import llm.client as L
    check_true("chaîne multi-fournisseurs", hasattr(L, "_providers_disponibles"))
    check_true("402 traduit", "gratuite" in L._explique("cerebras", Exception("Error code: 402 payment_required")))
    check_true("429 traduit", "limite" in L._explique("groq", Exception("429 rate limit")))
    check_true("404 traduit", "indisponible" in L._explique("gemini", Exception("404 model not found")))
    check_true("401 traduit", "invalide" in L._explique("groq", Exception("401 invalid api key")))


# ── 11b. Requêtes de recherche reformulées ────────────────────────────────────
def test_requetes():
    import agent.core as C
    import llm.client as L
    vrai = L.chat
    L.chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("hors ligne"))  # force le repli
    try:
        q = C.search_query("Quand se fait la rentrée pour les premières année à Pau à l'uppa ?")
        check_true("mots vides retirés", "quand" not in q.lower() and "pour" not in q.lower())
        check_true("noms propres gardés", "uppa" in q.lower() and "pau" in q.lower())
        check_true("pas de point d'interrogation", "?" not in q)
        check_true("requête courte", len(q.split()) <= 8)
        check_true("vide toléré", isinstance(C.search_query(""), str))
    finally:
        L.chat = vrai


# ── 11. Sécurité : confinement des fichiers ───────────────────────────────────
def test_securite():
    import re
    from pathlib import Path as P

    def nom_sur(fn):
        s = re.sub(r"[^A-Za-z0-9._-]", "_", (fn or "fichier"))[:80]
        return s.replace("..", "_").lstrip(".") or "fichier"

    for mauvais in ("../../etc/passwd", "..", "....//evil", ".hidden"):
        check_true(f"nom assaini: {mauvais}", "/" not in nom_sur(mauvais) and ".." not in nom_sur(mauvais))
    roots = [P("output").resolve(), P("data").resolve()]

    def autorise(p):
        t = P(p).resolve()
        return any(t == r or r in t.parents for r in roots)

    check_true("chemin légitime", autorise("output/visuels/x.png"))
    check("évasion bloquée", autorise("output/../main.py"), False)
    check("absolu bloqué", autorise("/etc/passwd"), False)
    check("préfixe trompeur bloqué", autorise("output-secret/x"), False)
    # Masquage des secrets dans les logs
    pat = re.compile(r"((?:key|api_key|token|password)=)[^&\s\"']+", re.I)
    check("clé masquée", pat.sub(r"\1***", "GET /agent/ask?key=SECRET&q=x"), "GET /agent/ask?key=***&q=x")

    # AUCUNE route ne doit être accessible sans clé (garde-fou pour les futures routes)
    src = (Path(__file__).resolve().parents[1] / "api" / "agent.py").read_text(encoding="utf-8")
    pos = [(m.start(), m.group(1).upper(), m.group(2))
           for m in re.finditer(r'@router\.(get|post|delete|put)\("([^"]+)"', src)]
    pos.append((len(src), "", ""))
    ouvertes = [f"{pos[i][1]} {pos[i][2]}" for i in range(len(pos) - 1)
                if "_check_key" not in src[pos[i][0]:pos[i + 1][0]]]
    check("aucune route publique", ouvertes, [])


# ── 15. Boucle asyncio jamais bloquée ────────────────────────────────────────
def test_non_bloquant():
    """Un appel réseau lent NE DOIT PAS geler la boucle : sinon plus aucun octet SSE
    ne part et Nova « réfléchit » indéfiniment (bug observé en production)."""
    import asyncio
    import time
    import re
    from pathlib import Path as P

    # 15a. La description du routage n'appelle JAMAIS le LLM (elle est calculée
    #      en amont du streaming, sur la boucle).
    src = (P(__file__).resolve().parents[1] / "api" / "agent.py").read_text(encoding="utf-8")
    corps = src.split("def _route_detail")[1].split("\ndef ")[0]
    for interdit in ("_resolve_app_action", "_llm_json", "chat(", "_event_text"):
        check(f"_route_detail sans {interdit}", interdit in corps, False)
    # Les sous-bulles sont écrites POUR L'UTILISATEUR : pas de jargon, pas de « action : … »
    for phrase, attendu in (("mes rdv de demain", "regarder"),
                            ("ajoute un rdv demain 14h", "ajouter"),
                            ("envoie un mail à Paul", "envoyer")):
        dit = A._intention_app(phrase)
        check_true(f"intention « {attendu} »", attendu in dit)
        check(f"pas de jargon dans « {dit} »", dit.startswith("action"), False)

    # 15b. run_agent_stream déporte bien ses appels bloquants dans un thread.
    core = (P(__file__).resolve().parents[1] / "agent" / "core.py").read_text(encoding="utf-8")
    check("safe_tool_call jamais direct", re.search(r"^\s+\w* ?=? ?safe_tool_call\(", core, re.M), None)
    check("search_query jamais direct", re.search(r"^\s+_q = search_query\(", core, re.M), None)

    # 15c. Preuve à l'exécution : pendant qu'un outil bloque 0,4 s, la boucle
    #      continue de tourner (un tick asyncio parallèle doit avancer).
    from agent.core import _off

    async def scenario():
        ticks = [0]

        async def horloge():
            while True:
                await asyncio.sleep(0.02)
                ticks[0] += 1

        t = asyncio.create_task(horloge())
        await _off(time.sleep, 0.4)          # « recherche web » lente
        t.cancel()
        return ticks[0]

    check_true("boucle libre pendant un appel bloquant", asyncio.run(scenario()) >= 5)


# ── 16. Aucun appel LLM ne peut durer indéfiniment ───────────────────────────
def test_delais():
    """Les SDK OpenAI/Groq attendent 600 s par défaut : sans délai explicite, un seul
    fournisseur muet suffit à faire « réfléchir » Nova pendant des dizaines de minutes."""
    import re
    from pathlib import Path as P
    import llm.client as C

    src = (P(__file__).resolve().parents[1] / "llm" / "client.py").read_text(encoding="utf-8")
    # « = OpenAI(… ) » : une vraie construction de client, pas le nom dans un message d'erreur
    clients = re.findall(r"=\s*((?:OpenAI|Groq)\((?:[^()]|\([^()]*\))*\))", src)
    check_true("des clients LLM sont bien construits", len(clients) >= 5)
    sans = [c[:45] for c in clients if "timeout=" not in c]
    check("tout client LLM a un délai", sans, [])
    sans_r = [c[:45] for c in clients if "max_retries" not in c]
    check("tout client LLM borne ses reprises", sans_r, [])

    check_true("délai unitaire raisonnable", 5 <= C.TIMEOUT_LLM <= 120)
    check_true("délai streaming raisonnable", 5 <= C.TIMEOUT_STREAM <= 180)
    check_true("budget global borné", 10 <= C.TIMEOUT_CHAINE <= 180)
    # La chaîne complète ne doit jamais dépasser l'échéance de l'agent de beaucoup,
    # sinon une seule itération ReAct mangerait tout le temps imparti.
    from config import config as CFG
    check_true("budget chaîne ≤ 2× échéance agent", C.TIMEOUT_CHAINE <= 2 * CFG.AGENT_TIMEOUT)

    # La chaîne s'arrête net quand le budget est épuisé (pas d'essai en cascade sans fin).
    import time
    appels = []

    def _lent(messages, modele, temperature, niveau=None):
        appels.append(modele)
        time.sleep(0.35)
        raise RuntimeError("fournisseur muet")

    vrai_chaine, vrai_budget = C._providers_disponibles, C.TIMEOUT_CHAINE
    try:
        C._providers_disponibles = lambda niveau="equilibre": [(f"faux{i}", _lent, "m") for i in range(20)]
        C.TIMEOUT_CHAINE = 0.8
        try:
            C.chat([{"role": "user", "content": "test"}])
        except Exception:
            pass
    finally:
        C._providers_disponibles, C.TIMEOUT_CHAINE = vrai_chaine, vrai_budget
    check_true(f"chaîne coupée net ({len(appels)} essais au lieu de 20)", 1 <= len(appels) <= 5)

    # Un fournisseur muet ne doit PAS empêcher les suivants d'être essayés.
    # Panne réelle : « nvidia : Request timed out. · délai global dépassé — fournisseurs
    # suivants non essayés », alors que Groq répondait parfaitement.
    def scenario_panne():
        essais = []

        def muet(messages, modele, temperature, niveau=None):
            essais.append("nvidia")
            time.sleep(min(C._BUDGET_APPEL.get(0.0), 5))
            raise RuntimeError("Request timed out.")

        def bon(messages, modele, temperature, niveau=None):
            essais.append("groq"); return "réponse de Groq"

        chaine = [("nvidia", muet, "m"), ("groq", bon, "m"), ("gemini", bon, "m")]
        C._providers_disponibles = lambda niveau="equilibre": (
            [c for c in chaine if not C._fournisseur_hs(c[0])] +
            [c for c in chaine if C._fournisseur_hs(c[0])])
        return essais

    sauve = (C.TIMEOUT_LLM, C.TIMEOUT_CHAINE, C._KO_FOURNISSEUR_TTL, dict(C._FOURNISSEURS_KO),
             C._DERNIER_OK["nom"], C._KO_LENT_TTL)
    try:
        C.TIMEOUT_LLM, C.TIMEOUT_CHAINE = 3.0, 9.0
        C._FOURNISSEURS_KO.clear(); C._DERNIER_OK["nom"] = ""
        essais = scenario_panne()
        t0 = time.monotonic()
        rep = C.chat([{"role": "user", "content": "x"}])
        duree = time.monotonic() - t0
        check("le fournisseur suivant prend le relais", rep, "réponse de Groq")
        check("les deux ont bien été essayés", essais, ["nvidia", "groq"])
        check_true(f"un mort ne mange pas tout le budget ({duree:.1f}s < 9s)", duree < 9.0)

        # Disjoncteur : au message suivant, le fournisseur en panne est écarté d'emblée.
        essais.clear()
        t0 = time.monotonic()
        C.chat([{"role": "user", "content": "y"}])
        check("fournisseur en panne écarté au 2e appel", essais, ["groq"])
        check_true("2e appel immédiat", time.monotonic() - t0 < 1.0)

        # …mais il retrouve sa chance quand la mise à l'écart expire.
        # Un simple dépassement de délai a sa PROPRE durée, bien plus courte qu'une
        # panne franche : un fournisseur lent n'est pas un fournisseur mort.
        check_true("sanction « lent » plus courte que « panne franche »",
                   C._KO_LENT_TTL < C._KO_FOURNISSEUR_TTL)
        C._KO_FOURNISSEUR_TTL = C._KO_LENT_TTL = 0.01
        time.sleep(0.05)
        essais.clear()
        C.chat([{"role": "user", "content": "z"}])
        check_true("seconde chance après expiration", "nvidia" in essais)
    finally:
        (C.TIMEOUT_LLM, C.TIMEOUT_CHAINE, C._KO_FOURNISSEUR_TTL) = sauve[:3]
        C._FOURNISSEURS_KO.clear(); C._FOURNISSEURS_KO.update(sauve[3])
        C._DERNIER_OK["nom"], C._KO_LENT_TTL = sauve[4], sauve[5]
        C._providers_disponibles = vrai_chaine

    # Un délai par fournisseur qui dépasserait le budget global rend la chaîne inutile.
    check_true("délai unitaire compatible avec le budget global",
               C.TIMEOUT_LLM * 2 <= C.TIMEOUT_CHAINE)
    check_true("au moins 3 fournisseurs peuvent être essayés", C.MIN_ESSAIS >= 3)

    # Un fournisseur qui ne répond JAMAIS ne doit pas figer l'itération de l'agent :
    # llm_call rend la main avec une erreur claire (c'est le bug « il réfléchit sans fin »).
    import asyncio
    import agent.core as AC

    async def jamais():
        vrai = C.chat
        C.chat = lambda *a, **k: time.sleep(60) or "trop tard"
        C.TIMEOUT_CHAINE = -19.5          # → plafond effectif de 0,5 s
        try:
            t0 = time.monotonic()
            try:
                await AC.llm_call([{"role": "user", "content": "x"}])
                return "aucune erreur", 0
            except TimeoutError:
                return "TimeoutError", time.monotonic() - t0
        finally:
            C.chat, C.TIMEOUT_CHAINE = vrai, vrai_budget

    genre, duree = asyncio.run(jamais())
    check("fournisseur muet → erreur, pas de blocage", genre, "TimeoutError")
    check_true(f"rendu en {duree:.2f}s", duree < 5)


# ── 17. Mode Cours ────────────────────────────────────────────────────────────
def test_cours():
    """Un cours de 2 h ne se rejoue pas : chaque brique doit être juste du premier coup."""
    import shutil
    from pathlib import Path as P
    from agent import cours

    # Bac à sable : on ne touche pas aux vraies sessions
    vrai_dir = cours._DIR
    cours._DIR = P("data/_test_cours")
    shutil.rmtree(cours._DIR, ignore_errors=True)
    try:
        # 17a. Recollage des tranches : le recouvrement volontaire ne doit pas laisser de doublon
        check("recollage simple",
              cours.recoller("le prof a dit que la dérivée", "la dérivée de x carré est deux x"),
              "de x carré est deux x")
        check("aucun recouvrement", cours.recoller("bonjour à tous", "ouvrez vos cahiers"),
              "ouvrez vos cahiers")
        check("tranche vide", cours.recoller("texte", ""), "")
        check("première tranche", cours.recoller("", "premier mot"), "premier mot")
        # Un seul mot commun est fortuit (« et », « de »…) : on ne coupe pas.
        check("un mot commun ne coupe pas", cours.recoller("il parle de", "de toute façon"),
              "de toute façon")
        # Ponctuation et majuscules ne doivent pas empêcher de reconnaître le doublon
        check("recollage malgré la ponctuation",
              cours.recoller("c'est très important !", "Très important, retenez bien ça"),
              "retenez bien ça")
        # Whisper redit parfois TOUTE une phrase de contexte : la fenêtre doit l'absorber.
        # Une fenêtre trop courte laissait passer 23 doublons sur une simulation de 2 h.
        longue = ("nous voyons le théorème des valeurs intermédiaires et retenez bien "
                  "que le résultat vaut quarante-deux unités")
        check("recollage d'une phrase entière répétée",
              cours.recoller("blabla " + longue, longue + " puis on enchaîne"),
              "puis on enchaîne")

        # 17b. Identifiant de session : aucune traversée de dossier possible
        for mauvais in ("../../etc/passwd", "..", "a/../../b", "'; DROP TABLE"):
            try:
                s = cours._sid_sur(mauvais)
                check_true(f"sid assaini: {mauvais}", "/" not in s and "." not in s)
            except ValueError:
                check_true(f"sid rejeté: {mauvais}", True)

        # 17c. Cycle de vie complet, sans réseau (LLM et Whisper simulés)
        import llm.client as C
        vrai_chat, vrai_transcrire = C.chat, cours.transcrire
        appels = {"n": 0}

        def faux_chat(messages, temperature=0.7, num_ctx=4096, niveau="equilibre", patience=0):
            appels["n"] += 1
            sys = (messages[0].get("content") or "")
            if "JSON STRICT" in sys:
                return '{"fiches":[{"q":"Quelle est la dérivée de x² ?","r":"2x.","theme":"Dérivées"}]}'
            return "## En bref\nCours sur les dérivées.\n\n## Le cours\n### Définition\nLa **dérivée**."

        cours.transcrire = lambda audio, nom="x.webm": "la dérivée de x carré vaut deux x"
        C.chat = faux_chat
        try:
            s = cours.demarrer("Les dérivées", "Maths")
            sid = s["id"]
            for _ in range(3):
                cours.ajouter_tranche(sid, b"audio-factice", 60.0)
            etat = cours._lire(sid)
            check("3 tranches enregistrées", etat["segments"], 3)
            check_true("doublons retirés par le recollage",
                       etat["transcript"].count("dérivée de x carré") == 1)
            check("audio jamais écrit sur le disque",
                  [p.name for p in cours._DIR.glob("*") if p.suffix not in (".json", ".tmp")], [])

            fin = cours.terminer(sid)
            check_true("synthèse produite", "En bref" in fin["synthese"])
            check("fiches produites", len(fin["fiches"]), 1)
            check("session terminée", fin["etat"], "termine")

            md = cours.markdown(sid)
            for attendu in ("# Les dérivées", "Maths", "Fiches de révision", "dérivée de x²"):
                check_true(f"export contient « {attendu} »", attendu in md)

            check("session listée", [x["id"] for x in cours.lister()], [sid])
            check_true("suppression", cours.supprimer(sid) and not cours.lister())

            # 17d. Une tranche illisible ne doit PAS interrompre le cours : trou signalé, écoute continue
            s2 = cours.demarrer("Cours cassé")
            cours.transcrire = lambda audio, nom="x.webm": (_ for _ in ()).throw(RuntimeError("whisper HS"))
            try:
                cours.ajouter_tranche(s2["id"], b"zz", 60.0)
            except Exception:
                pass
            e2 = cours._lire(s2["id"])
            check("trou enregistré", len(e2["trous"]), 1)
            check("session toujours en cours", e2["etat"], "en_cours")
            cours.transcrire = lambda audio, nom="x.webm": "on reprend le cours ici"
            cours.ajouter_tranche(s2["id"], b"zz", 60.0)
            check_true("l'écoute a repris après l'échec",
                       "on reprend le cours" in cours._lire(s2["id"])["transcript"])
            cours.supprimer(s2["id"])
        finally:
            C.chat, cours.transcrire = vrai_chat, vrai_transcrire

        # 17e. Garde-fou clé : on doit refuser AVANT d'enregistrer 2 h, pas après.
        vrai_t, vrai_dispo = cours.transcrire, cours.transcription_dispo
        try:
            cours.transcription_dispo = lambda: True
            cours._VERIF.update(t=0.0, res=None)
            cours.transcrire = lambda a, n="t.wav": (_ for _ in ()).throw(
                RuntimeError("Error code: 401 - Invalid API Key"))
            ok, msg = cours.verifier_transcription()
            check("clé refusée → départ bloqué", ok, False)
            check_true("message explicite sur la clé", "clé" in msg.lower())

            cours._VERIF.update(t=0.0, res=None)
            cours.transcrire = lambda a, n="t.wav": (_ for _ in ()).throw(
                RuntimeError("Error code: 429 rate limit reached"))
            ok, msg = cours.verifier_transcription()
            check("quota atteint → départ bloqué", ok, False)
            check_true("message explicite sur le quota", "quota" in msg.lower())

            cours._VERIF.update(t=0.0, res=None)
            recu = {}
            cours.transcrire = lambda a, n="t.wav": recu.update(taille=len(a), nom=n) or ""
            check("Whisper répond → départ autorisé", cours.verifier_transcription(), (True, ""))
            check_true("le bip de test est un vrai WAV", 2000 < recu["taille"] < 60000)
            check("le bip est envoyé en .wav", recu["nom"], "test.wav")
        finally:
            cours.transcrire, cours.transcription_dispo = vrai_t, vrai_dispo
            cours._VERIF.update(t=0.0, res=None)

        # 17f. La condensation ne se déclenche qu'au-delà du seuil, et le budget final est borné
        check_true("seuil de condensation raisonnable", 2000 <= cours.SEUIL_CONDENSE <= 20000)
        check_true("budget final borné", 4000 <= cours.BUDGET_FINAL <= 30000)
        check_true("durée max bornée", 3600 <= cours.DUREE_MAX <= 8 * 3600)
    finally:
        shutil.rmtree(cours._DIR, ignore_errors=True)
        cours._DIR = vrai_dir


# ── 18. Ne jamais jeter ce qui a été trouvé ──────────────────────────────────
def test_trouvailles():
    """Cas réel : Nova trouve Le Monde, dépasse le délai, et répondait « reformule
    ta question » en jetant des résultats pourtant valides."""
    import asyncio
    import agent.core as AC

    RES = ("🔎 **Résultats web : actu tech** (6)\n\n**1. Actualités du jour - Le Monde**\n"
           "_lemonde.fr_\n🔗 https://lemonde.fr/tech")

    # 18a. Le repli rend les sources réelles, sans doublon ni excuse sèche
    r = AC._repli_observations([RES, RES])
    check_true("le repli cite la source réelle", "lemonde.fr" in r)
    check("le repli ne répète pas deux fois la même trouvaille", r.count("Le Monde"), 1)
    check("aucun repli sans trouvaille", AC._repli_observations([]), "")
    check("un échec d'outil n'est pas une trouvaille",
          AC._repli_observations(["[Self-heal] Outil 'search_web' a échoué"]), "")
    check("un moteur muet n'est pas une trouvaille",
          AC._repli_observations(["⚠️ Aucun résultat exploitable pour « x »"]), "")
    # La consigne interne injectée par le plafond ne doit pas fuiter à l'écran
    check_true("la consigne système ne fuit pas",
               "[SYSTÈME]" not in AC._repli_observations([RES + "\n\n[SYSTÈME] N'en lance plus"]))

    # 18b. Le protocole ReAct ne doit JAMAIS s'afficher dans le chat
    check("action brute rejetée",
          AC._texte_lisible('THOUGHT: je cherche\nACTION: search_web\nPARAMS: {"query": "x"}'), "")
    check("préfixes retirés", AC._texte_lisible("FINAL: Voici la réponse."), "Voici la réponse.")
    check("prose normale intacte", AC._texte_lisible("Voici la réponse."), "Voici la réponse.")

    # 18c. Deux formulations équivalentes = une seule recherche
    check("clés équivalentes",
          AC._cle_requete("actu tech du jour !") == AC._cle_requete("Actu, TECH jour"), True)
    check_true("clés distinctes si sujet différent",
               AC._cle_requete("actu tech") != AC._cle_requete("actu sport"))

    # 18d. Bout en bout : requête identique relancée → aucun second appel réseau,
    #      et au-delà du plafond la recherche est refusée.
    appels = []

    def outil(loader, nom, params, fallback="", echeance=0.0):
        appels.append(params.get("query", ""))
        return RES

    tours = {"n": 0}

    async def faux_llm(messages, model=None, temperature=0.7, timeout=0.0):
        tours["n"] += 1
        if tours["n"] <= 4:      # le modèle s'obstine à relancer des recherches
            return ('THOUGHT: je cherche\nACTION: search_web\n'
                    'PARAMS: {"query": "actu tech du jour"}')
        return "FINAL: Voici l'actualité tech."

    # safe_tool_call est importé DANS la fonction : c'est le module source qu'il faut remplacer
    import agent.self_heal as SH
    vrais = (SH.safe_tool_call, AC.llm_call, AC.search_query)
    try:
        SH.safe_tool_call = outil
        AC.llm_call = faux_llm
        AC.search_query = lambda t: "actu tech du jour"

        async def run():
            cfg = {"name": "Nova", "tools": ["search_web"], "force_search": True,
                   "system_prompt": "test"}
            out = []
            async for step in AC.run_agent_stream("actu tech ?", cfg, "test_trouv"):
                out.append(step)
            return out

        etapes = asyncio.run(run())
        finale = [e for e in etapes if e["type"] == "final"]
        check("une seule recherche réseau malgré les relances", len(appels), 1)
        check_true("réponse finale rendue", finale and finale[0]["answer"])
        check_true("le protocole ne fuit pas dans la réponse",
                   "ACTION:" not in (finale[0]["answer"] if finale else ""))
    finally:
        SH.safe_tool_call, AC.llm_call, AC.search_query = vrais

    # 18e. safe_tool_call cesse de retenter une fois l'échéance passée
    import time
    from agent.self_heal import safe_tool_call as vrai_stc

    class LoaderKO:
        def run(self, nom, params):
            essais.append(1)
            raise RuntimeError("réseau lent")

    essais = []
    vrai_stc(LoaderKO(), "search_web", {}, "", time.monotonic() - 1)
    check("échéance dépassée → une seule tentative", len(essais), 1)
    essais = []
    vrai_stc(LoaderKO(), "search_web", {}, "", time.monotonic() + 60)
    check("échéance lointaine → 3 tentatives", len(essais), 3)

    # 18f. Une recherche web est bornée dans le temps
    from plugins.builtin import web_search as WS
    check_true("budget de recherche borné", 8 <= WS.BUDGET_RECHERCHE <= 40)
    check_true("délai par hôte court", 3 <= WS.TIMEOUT_HOTE <= 12)
    check_true("plafond de recherches bas", 1 <= AC.MAX_RECHERCHES <= 4)
    check_true("synthèse de secours courte", 10 <= AC._SYNTHESE_TIMEOUT <= 40)


# ── 19. Qualité de la requête de recherche ───────────────────────────────────
def test_requete_web():
    """« Résume l'actu tech du jour » cherché tel quel ramenait un PODCAST intitulé
    « L'Actu Tech — chaque jour Patrick résume… » au lieu de l'actualité."""
    from datetime import datetime
    import agent.core as AC
    from agent.core import requete_simple, veut_actualite

    auj = datetime.now()
    date = AC._JOURS_COURT(auj)

    # Les verbes de commande ne doivent jamais partir au moteur de recherche
    for phrase in ("Résume l'actu tech du jour", "donne moi les news tech",
                   "explique moi la photosynthèse", "dis-moi la météo à Pau"):
        q = requete_simple(phrase).lower()
        for verbe in ("résume", "resume", "donne", "explique", "dis"):
            check(f"« {verbe} » retiré de « {phrase[:22]}… »", verbe in q.split(), False)

    # Une demande d'actualité DOIT être datée, sinon le moteur ramène du générique
    check("actu du jour datée", requete_simple("Résume l'actu tech du jour"),
          f"actualité tech {date}")
    check("prix du jour daté", requete_simple("prix du bitcoin aujourd hui"),
          f"prix bitcoin {date}")
    check("récent → année", requete_simple("donne moi les news tech"), f"news tech {auj.year}")
    # …et une question intemporelle ne doit PAS l'être
    check("question intemporelle non datée", requete_simple("explique moi la photosynthèse"),
          "photosynthèse")
    check_true("date absente d'une question intemporelle",
               str(auj.year) not in requete_simple("qui a gagné la coupe du monde 1998"))

    # Élisions et tournures ne laissent pas de résidu
    check_true("élision retirée", not requete_simple("Résume l'actu du jour").startswith("l'"))
    check_true("« quoi de neuf » sans résidu",
               "neuf" not in requete_simple("quoi de neuf sur l'IA").lower())
    check("apostrophe typographique gérée",
          requete_simple("l’actu du jour"), f"actualité {date}")
    check_true("« aujourd hui » sans apostrophe reconnu", veut_actualite("prix du bitcoin aujourd hui"))
    # Les noms propres gardent leur casse
    check("noms propres préservés", requete_simple("rentrée UPPA Pau licence eco gestion"),
          "rentrée UPPA Pau licence eco gestion")
    check("entrée vide", requete_simple(""), "")

    # Intention actualité → recherche dans les actualités
    check_true("actu détectée", veut_actualite("résume l'actu du jour"))
    check("cours non pris pour de l'actu", veut_actualite("explique la photosynthèse"), False)

    # Les résultats gonflés (même paragraphe répété) sont dégonflés
    from plugins.builtin.web_search import _extrait
    bloc = "L'Actu Tech c'est chaque jour une info importante. Mes podcasts hebdo. " * 3
    court = _extrait(bloc)
    check("répétition supprimée", court.count("Mes podcasts hebdo"), 1)
    check_true("extrait borné", len(court) <= 300)
    check("extrait vide géré", _extrait(""), "")
    check("balises html retirées", _extrait("<b>Titre</b> et <i>suite</i>."), "Titre et suite.")


# ── 20. Saturation des offres gratuites ──────────────────────────────────────
def test_saturation():
    """Panne réelle : les 4 fournisseurs en échec, dont « groq : offre gratuite épuisée
    (paiement demandé) » — alors que Groq était seulement à sa limite du moment."""
    import time
    import types
    import llm.client as C

    # Message d'erreur RÉEL de Groq : il contient un lien « …/settings/billing »
    ERR = ("Error code: 429 - Rate limit reached for model `llama-3.3-70b-versatile` on "
           "tokens per minute (TPM): Limit 12000, Used 11800. Please try again in 7.5s. "
           "Need more? https://console.groq.com/settings/billing")

    # 20a. Diagnostic : une limite passagère ne doit PAS être annoncée comme un
    #      abonnement à payer — ça envoyait chercher une carte bancaire pour rien.
    diag = C._explique("groq", RuntimeError(ERR))
    check_true("limite annoncée comme limite", "limite" in diag.lower())
    check("pas de faux « paiement demandé »", "paiement" in diag.lower(), False)
    check("délai lu chez le fournisseur", C._delai_conseille(RuntimeError(ERR)), 7.5)
    check("délai « 2m30s » compris",
          C._delai_conseille(RuntimeError("please try again in 2m30s")), 150.0)
    check("aucun délai annoncé", C._delai_conseille(RuntimeError("boom")), 0.0)
    # Un vrai défaut de paiement reste bien identifié
    check_true("402 reste un défaut de paiement",
               "paiement" in C._explique("cerebras", RuntimeError("402 payment_required")).lower())

    # 20b. Chez Groq le quota est PAR MODÈLE : un 429 sur le 70B doit faire essayer le 8B
    essayes = []

    class FauxGroq:
        def __init__(self, **k): pass
        def with_options(self, **k): return self

        class models:
            @staticmethod
            def list(): raise RuntimeError("pas de liste")

        class chat:
            class completions:
                @staticmethod
                def create(model, messages, temperature, max_tokens):
                    essayes.append(model)
                    if "70b" in model:
                        raise RuntimeError(ERR)
                    R = type("R", (), {})
                    R.choices = [type("c", (), {"message": type("m", (), {"content": "ok"})()})()]
                    R.usage = None
                    return R()

    vrai_mod = sys.modules.get("groq")
    sys.modules["groq"] = types.ModuleType("groq")
    sys.modules["groq"].Groq = FauxGroq
    sauve_ok, sauve_ko = dict(C._MODELES_OK), dict(C._MODELES_KO)
    try:
        C._MODELES_OK.pop("groq", None); C._MODELES_KO.clear()
        rep = C._groq_chat([{"role": "user", "content": "x"}], "llama-3.3-70b-versatile", 0.2)
        check("modèle saturé → modèle plus léger", rep, "ok")
        check_true(f"deux modèles essayés ({essayes})", len(essayes) >= 2)
        check_true("le 70B a bien été tenté en premier", "70b" in essayes[0])
    finally:
        if vrai_mod is not None:
            sys.modules["groq"] = vrai_mod
        else:
            sys.modules.pop("groq", None)
        C._MODELES_OK.clear(); C._MODELES_OK.update(sauve_ok)
        C._MODELES_KO.clear(); C._MODELES_KO.update(sauve_ko)

    # 20c. Tout saturé = panne PASSAGÈRE, distincte d'une panne définitive
    vrai_chaine = C._providers_disponibles
    sauve = (dict(C._FOURNISSEURS_KO), C._DERNIER_OK["nom"])
    try:
        C._FOURNISSEURS_KO.clear(); C._DERNIER_OK["nom"] = ""
        C._providers_disponibles = lambda niveau="equilibre": [
            ("groq", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(ERR)), "m")]
        try:
            C.chat([{"role": "user", "content": "x"}])
            check("saturation signalée à part", "aucune exception", "ToutSature")
        except C.ToutSature as e:
            check("saturation signalée à part", type(e).__name__, "ToutSature")
            check("le délai remonte à l'appelant", e.delai, 7.5)
        except Exception as e:
            check("saturation signalée à part", type(e).__name__, "ToutSature")

        # Une panne DÉFINITIVE ne doit pas être confondue avec une saturation
        C._FOURNISSEURS_KO.clear()
        C._providers_disponibles = lambda niveau="equilibre": [
            ("groq", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("401 invalid api key")), "m")]
        try:
            C.chat([{"role": "user", "content": "x"}])
            check("panne définitive non confondue", "aucune exception", "RuntimeError")
        except C.ToutSature:
            check("panne définitive non confondue", "ToutSature", "RuntimeError simple")
        except Exception:
            OK.append(("panne définitive non confondue", True, True))

        # 20d. Avec de la patience, Nova attend que la limite se libère au lieu d'échouer
        debut = time.monotonic()

        def sature_20s(*a, **k):
            if time.monotonic() - debut < 0.6:      # « limite » qui se libère avec le temps
                raise RuntimeError("429 rate limit. Please try again in 0.2s")
            return "réponse enfin obtenue"

        C._FOURNISSEURS_KO.clear()
        C._providers_disponibles = lambda niveau="equilibre": [("groq", sature_20s, "m")]
        check("patience → succès", C.chat([{"role": "user", "content": "x"}], patience=4),
              "réponse enfin obtenue")
    finally:
        C._providers_disponibles = vrai_chaine
        C._FOURNISSEURS_KO.clear(); C._FOURNISSEURS_KO.update(sauve[0])
        C._DERNIER_OK["nom"] = sauve[1]

    # 20e. L'attente s'allonge à chaque tour : les limites Groq se comptent par MINUTE,
    #      réessayer trois fois après 5 s retomberait sur la même limite.
    from agent import cours
    check_true("le mode cours est patient", cours.PATIENCE >= 2)


# ── 21. La synthèse d'un cours ne bloque pas la requête HTTP ─────────────────
def test_synthese_fond():
    """Une synthèse peut durer plusieurs minutes (modèles saturés) : garder la requête
    ouverte la ferait couper par le navigateur, et le cours semblerait perdu."""
    import shutil
    import time
    from pathlib import Path as P
    from agent import cours
    import llm.client as C

    vrai_dir, vrai_t = cours._DIR, cours.transcrire
    cours._DIR = P("data/_test_fond")
    shutil.rmtree(cours._DIR, ignore_errors=True)
    vrai_chat = C.chat
    try:
        cours.transcrire = lambda a, n="t.webm": "l'effet Doppler décale la fréquence perçue"
        depart = [0.0]

        def lent(messages, temperature=0.7, num_ctx=4096, niveau="equilibre", patience=0):
            time.sleep(0.4)                       # la synthèse prend du temps
            if "JSON STRICT" in (messages[0].get("content") or ""):
                return '{"fiches":[{"q":"Doppler ?","r":"Décalage.","theme":"Ondes"}]}'
            return "## En bref\nCours sur l'effet Doppler."

        C.chat = lent
        s = cours.demarrer("effet doppler", "physique chimie")
        cours.ajouter_tranche(s["id"], b"audio", 60.0)

        t0 = time.monotonic()
        vue = cours.lancer_synthese(s["id"])
        rendu = time.monotonic() - t0
        check_true(f"la main est rendue tout de suite ({rendu:.2f}s)", rendu < 0.3)
        check("état « traitement » pendant le travail", vue["etat"], "traitement")

        # Relancer pendant que ça tourne ne doit pas lancer un second travail
        cours.lancer_synthese(s["id"])
        check("pas de synthèse en double", len([t for t in cours._TRAVAUX.values() if t.is_alive()]), 1)

        for _ in range(60):                        # l'UI interroge l'état
            time.sleep(0.2)
            if cours._lire(s["id"]).get("synthese"):
                break
        fin = cours._lire(s["id"])
        check("synthèse aboutie en tâche de fond", fin["etat"], "termine")
        check_true("contenu produit", "Doppler" in fin["synthese"])
        check("fiches produites", len(fin["fiches"]), 1)

        # Rappeler après coup rend le résultat sans refaire le travail
        check("relance idempotente", cours.lancer_synthese(s["id"])["synthese"], fin["synthese"])

        # Si la condensation a échoué (modèles saturés), la transcription arrive en UN
        # seul bloc géant : il doit être découpé, pas tronqué — sinon la fin du cours
        # disparaît en silence.
        vus = []

        def fusion(messages, temperature=0.7, num_ctx=4096, niveau="equilibre", patience=0):
            vus.append(messages[1]["content"])
            return messages[1]["content"][:800]

        C.chat = fusion
        gros = " ".join(f"phrase{i} contenu du cours" for i in range(4000))   # ≈ 100 000 car.
        res = cours._reduire([gros])
        check_true(f"bloc géant découpé ({len(vus)} morceaux)", len(vus) > 1)
        check_true("le début du cours est traité", any("phrase0 " in v for v in vus))
        check_true("la FIN du cours est traitée aussi", any("phrase3999" in v for v in vus))
        check_true("résultat dans le budget", len(res) <= cours.BUDGET_FINAL)
    finally:
        C.chat = vrai_chat
        cours.transcrire = vrai_t
        shutil.rmtree(cours._DIR, ignore_errors=True)
        cours._DIR = vrai_dir


# ── 22. Aucun modèle disponible : rendre quand même les trouvailles ──────────
def test_sans_modele():
    """Cas réel : les 4 fournisseurs morts, mais la recherche avait ramené dcod.ch.
    Nova affichait « ❌ LLM indisponible: … » et jetait l'article."""
    import asyncio
    import agent.core as AC
    import agent.self_heal as SH

    RES = ("🔎 **Résultats web : actualité technologie 19 août 2026** (6)\n\n"
           "**1. Menaces & IA : 15 dérives et avancées clés du 19 août 2026**\n_dcod.ch_\n"
           "🔗 https://dcod.ch/2026/08/19/menaces-ia")
    PANNE = RuntimeError("Aucun modèle disponible pour le moment.\n"
                         "• groq : offre gratuite épuisée (paiement demandé)\n"
                         "• nvidia : Request timed out.")

    async def llm_mort(messages, model=None, temperature=0.7, timeout=0.0):
        raise PANNE

    vrais = (SH.safe_tool_call, AC.llm_call, AC.search_query)
    try:
        SH.safe_tool_call = lambda l, n, p, f="", e=0.0: RES
        AC.llm_call = llm_mort
        AC.search_query = lambda t: "actualité technologie 19 août 2026"

        async def run():
            cfg = {"name": "Nova", "tools": ["search_web"], "force_search": True,
                   "system_prompt": "test"}
            out = []
            async for step in AC.run_agent_stream("actu tech du jour ?", cfg, "test_sm"):
                out.append(step)
            return out

        finale = [e for e in asyncio.run(run()) if e["type"] == "final"]
        rep = finale[0]["answer"] if finale else ""
        check_true("l'article trouvé est rendu", "dcod.ch" in rep)
        check_true("le lien est conservé", "https://dcod.ch" in rep)
        check("aucune erreur technique affichée", "LLM indisponible" in rep, False)
        check("pas de liste de fournisseurs en panne", "offre gratuite épuisée" in rep, False)
        check_true("le motif est le bon (pas « pas eu le temps »)",
                   "Aucun modèle n'est disponible" in rep)

        # …mais si RIEN n'a été trouvé, le message doit rester utile et actionnable
        SH.safe_tool_call = lambda l, n, p, f="", e=0.0: "⚠️ Aucun résultat exploitable pour « x »"
        finale = [e for e in asyncio.run(run()) if e["type"] == "final"]
        rep2 = finale[0]["answer"] if finale else ""
        check_true("message actionnable", "clé" in rep2.lower() or "réessaie" in rep2.lower())
        check("pas de trace brute en tête", rep2.startswith("❌ LLM indisponible"), False)
    finally:
        SH.safe_tool_call, AC.llm_call, AC.search_query = vrais

    # Le message d'erreur ultime doit dire QUOI FAIRE
    check_true("limite → « réessaie »",
               "réessaie" in AC._erreur_lisible(RuntimeError("429 rate limit")).lower())
    check_true("panne de clé → où en régénérer une",
               "groq.com" in AC._erreur_lisible(RuntimeError("groq : clé invalide")).lower())


# ── 23. Les décisions s'affichent une par une, en français ───────────────────
def test_bulles_live():
    """« la même bulle qui s'ouvre d'un coup avec des mots incompréhensibles » :
    trois étiquettes de jargon envoyées en un seul message."""
    bulles = A._route_bulles("Résume l'actu tech du jour")
    check_true("plusieurs décisions distinctes", len(bulles) >= 2)
    for b in bulles:
        check(f"pas de jargon : « {b} »",
              any(j in b.lower() for j in ("requise", "factuelle", "action :", "toolkit",
                                           "slug", "niveau", "routage")), False)
        check_true(f"phrase courte : « {b} »", len(b) <= 44)
    check_true("l'intention de recherche est dite simplement",
               any("cherch" in b for b in bulles))
    # Le niveau de modèle aussi doit être compréhensible
    for niv, texte in A._NIVEAU_BULLE.items():
        check(f"niveau « {niv} » sans jargon", "modèle équilibré" in texte, False)
    # Chaque type de demande a ses propres bulles, jamais les mêmes partout
    distinctes = {" ".join(A._route_bulles(m)) for m in
                  ("salut", "mon agenda demain", "explique la photosynthèse",
                   "résume l'actu du jour", "fais-moi un visuel")}
    check_true(f"bulles adaptées à la demande ({len(distinctes)} variantes)", len(distinctes) >= 4)
    # Robustesse
    check_true("message vide géré", isinstance(A._route_bulles(""), list))


# ── 24. Diagnostic des modèles ───────────────────────────────────────────────
def test_diagnostic():
    """Quand Nova dit « aucun modèle disponible », il faut savoir LAQUELLE des clés
    pose problème — sans avoir à deviner ni à lire les logs Render."""
    import time
    import llm.client as C
    from config import config as CFG

    sauve = {n: getattr(CFG, f"{n.upper()}_API_KEY", "") for n in
             ("nvidia", "groq", "gemini", "openrouter", "cerebras", "xai")}
    vraie_chaine = C._providers_disponibles
    vrai_to = C.TIMEOUT_LLM
    try:
        CFG.NVIDIA_API_KEY = "nvapi-cle-valide-mais-lente-xxxxxxxx"
        CFG.GROQ_API_KEY = "gsk_cle_qui_marche_xxxxxxxxxxxxxxxxx"
        CFG.GEMINI_API_KEY = "AIzaCleApiNonActivee_xxxxxxxxxxxx"
        CFG.OPENROUTER_API_KEY = "meta-llama/llama-3.3-70b"     # nom de modèle collé par erreur
        CFG.CEREBRAS_API_KEY = CFG.XAI_API_KEY = ""
        C.TIMEOUT_LLM = 0.5

        def lent(m, mo, t, n=None):
            time.sleep(0.8); return "ok"

        def bon(m, mo, t, n=None):
            return "ok"

        def g403(m, mo, t, n=None):
            raise RuntimeError("403 Forbidden — Generative Language API has not been used")

        def o401(m, mo, t, n=None):
            raise RuntimeError("Error code: 401 - No auth credentials found")

        C._providers_disponibles = lambda niveau="equilibre": [
            ("nvidia", lent, "m"), ("groq", bon, "m"),
            ("gemini", g403, "m"), ("openrouter", o401, "m")]
        C._FOURNISSEURS_KO.clear()

        d = {r["fournisseur"]: r for r in (A._diag_un_llm(n) for n in
                                           ("nvidia", "groq", "gemini", "openrouter"))}

        # Un fournisseur LENT mais fonctionnel doit être reconnu comme tel, pas « en panne »
        check("fournisseur lent : marche quand même", d["nvidia"]["ok"], True)
        check_true("lenteur signalée", "LENTEMENT" in d["nvidia"]["conseil"])
        check_true("le seuil cité est le vrai délai appliqué",
                   str(C.TIMEOUT_LLM) in d["nvidia"]["conseil"])
        # Un fournisseur sain n'inquiète pas inutilement
        check("fournisseur sain : rien à signaler", d["groq"]["conseil"], "")
        check("fournisseur sain : ok", d["groq"]["ok"], True)
        # Les pannes sont expliquées, pas juste constatées
        check_true("403 → API non activée", "activ" in d["gemini"]["conseil"])
        check_true("clé mal collée détectée AVANT l'appel",
                   "sk-or-" in d["openrouter"]["conseil"])
        # Une clé absente n'est pas une panne
        CFG.NVIDIA_API_KEY = ""
        vide = A._diag_un_llm("nvidia")
        check("clé absente ≠ panne", vide["cle_presente"], False)
        check_true("dit qu'il n'y a pas de clé", "clé" in vide["conseil"])

        # 🔒 Une clé ne doit JAMAIS ressortir en entier d'un diagnostic
        CFG.GROQ_API_KEY = "gsk_secret_a_ne_pas_divulguer_123456789"
        r = A._diag_un_llm("groq")
        check("clé jamais exposée en entier", CFG.GROQ_API_KEY in json.dumps(r), False)
        check_true("aperçu court", len(r.get("cle_apercu", "")) <= 15)

        # Un espace collé par erreur est la panne la plus invisible
        CFG.GROQ_API_KEY = "gsk_cle avec espace"
        check_true("espace dans la clé signalé", "espace" in A._diag_un_llm("groq")["conseil"])
    finally:
        for n, v in sauve.items():
            setattr(CFG, f"{n.upper()}_API_KEY", v)
        C._providers_disponibles = vraie_chaine
        C.TIMEOUT_LLM = vrai_to
        C._FOURNISSEURS_KO.clear()


# ── 25. Qualité de l'actualité ───────────────────────────────────────────────
def test_actualite():
    """Cas réel : « résume l'actu tech du jour » rendait « Wel-Bloom Bio-Tech présentera
    des jelly fonctionnels » et « Ashtead Technology a publié une mise à jour de
    trading » — des communiqués financiers, pas de l'actu tech."""
    from datetime import datetime, timedelta, timezone
    from plugins.builtin import actu_rss as R

    maintenant = datetime.now(timezone.utc)
    recent = (maintenant - timedelta(hours=3)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    vieux = (maintenant - timedelta(days=9)).strftime("%a, %d %b %Y %H:%M:%S +0000")

    rss = ('<?xml version="1.0"?><rss version="2.0"><channel>'
           '<item><title>Apple devoile son casque</title><link>https://f.fr/a</link>'
           '<description>&lt;p&gt;Presentation &lt;b&gt;a Cupertino&lt;/b&gt;.&lt;/p&gt;</description>'
           f'<pubDate>{recent}</pubDate></item>'
           '<item><title>Article de la semaine derniere</title><link>https://f.fr/v</link>'
           f'<pubDate>{vieux}</pubDate></item>'
           '</channel></rss>').encode()
    atom = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            '<entry><title>Apple dévoile son casque</title>'
            '<link href="https://n.fr/b"/><summary>La meme depeche.</summary>'
            f'<published>{(maintenant - timedelta(hours=2)).isoformat()}</published></entry>'
            '</feed>').encode()

    a = R.lire_flux(rss, "Frandroid")
    b = R.lire_flux(atom, "Numerama")
    check("RSS 2.0 lu", len(a), 2)
    check("Atom lu", len(b), 1)
    check("lien Atom (attribut href)", b[0]["lien"], "https://n.fr/b")
    check("balises HTML retirées du résumé", a[0]["resume"], "Presentation a Cupertino.")
    check_true("dates comprises", a[0]["date"] and a[1]["date"] < a[0]["date"])
    # La même dépêche reprise ailleurs, avec ou sans accent, ne sort qu'une fois
    check("dépêche dédoublonnée malgré l'accent",
          R._cle_titre("Apple devoile son casque"), R._cle_titre("Apple DÉVOILE son casque"))
    check_true("titres différents = clés différentes",
               R._cle_titre("Apple devoile") != R._cle_titre("Google devoile"))
    # Un flux cassé vaut zéro article, jamais une exception
    check("flux illisible ignoré", R.lire_flux(b"<pas du xml", "X"), [])
    check("flux vide ignoré", R.lire_flux(b"", "X"), [])

    # La question détermine les médias interrogés
    for q, attendu in (("résume l'actu tech du jour", "tech"), ("actu du jour", "general"),
                       ("actualité sport", "sport"), ("quoi de neuf en économie", "economie"),
                       ("les news sur l'espace", "science")):
        check(f"thème de « {q[:26]} »", R.theme_de(q), attendu)
    check_true("chaque thème a plusieurs médias",
               all(len(v) >= 2 for v in R.FLUX.values()))
    check_true("fraîcheur bornée", 12 <= R.FRAICHEUR_H <= 96)

    # Mise en forme : source et date visibles, sinon impossible de juger la fraîcheur
    rendu = R.formater(a[:1], "tech")
    check_true("source citée", "Frandroid" in rendu)
    check_true("date citée", "/" in rendu and "h" in rendu)
    check_true("lien cliquable", "https://f.fr/a" in rendu)
    check("aucun article → aucun rendu", R.formater([]), "")

    # ── La date littérale ne doit PLUS polluer une requête d'actualité ──
    import llm.client as C
    from agent.core import requete_simple
    auj = datetime.now()
    q_actu = requete_simple("Résume l'actu tech du jour", pour_actu=True)
    check("actu : pas de date littérale", str(auj.year) in q_actu, False)
    check("actu : sujet conservé", q_actu, "actualité tech")
    # …mais elle reste utile pour une recherche web classique
    check_true("web : date conservée",
               str(auj.year) in requete_simple("prix du bitcoin aujourd hui"))


if __name__ == "__main__":
    for fn in (test_routage, test_echecs, test_dates, test_titres, test_robustesse,
               test_visuels, test_profil, test_automatisations, test_escouade,
               test_caches, test_slugs, test_modeles, test_requetes, test_securite,
               test_non_bloquant, test_delais, test_cours, test_trouvailles,
               test_requete_web, test_saturation, test_synthese_fond,
               test_sans_modele, test_bulles_live, test_diagnostic, test_actualite):
        try:
            fn()
        except Exception as e:
            KO.append((fn.__name__, f"EXCEPTION {type(e).__name__}: {e}", "exécution complète"))
    print(f"\n{'='*66}\n  {len(OK)} tests OK   ·   {len(KO)} échec(s)\n{'='*66}")
    for nom, got, want in KO:
        print(f"  ❌ {nom}\n       obtenu : {got!r}\n       attendu: {want!r}")
    sys.exit(1 if KO else 0)
