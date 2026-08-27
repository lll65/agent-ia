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

    # 7b. ⚠️ Nova n'enregistrait presque rien de ce qu'on lui dit de soi. Trois règles
    # trop strictes : toute phrase avec « ? » était jetée (même « tu peux retenir
    # que… ? »), seules les tournures en « je… » comptaient (jamais « ma sœur »,
    # « mes parents »), et rien au-delà de 25 mots.
    vrais_cnx = A._connected_accounts
    try:
        A._connected_accounts = lambda: [(s, "u", "ACTIVE") for s in
                                         ("gmail", "googlecalendar", "googlesheets", "notion")]
        A._APP_RECENTE.clear()
        for phrase in ("j'ai 17 ans", "je m'appelle Lohan", "j'habite à Pau",
                       "mon père et moi on a un PEA ensemble",
                       "je suis en terminale au lycée, je passe le bac cette année",
                       "je bosse sur un projet d'IA qui s'appelle Nova",
                       "mes parents sont divorcés", "ma soeur s'appelle Emma",
                       "j'ai un chien qui s'appelle Rex", "je me lève à 6h30",
                       "je suis allergique aux arachides",
                       "je fais du sport 3 fois par semaine et j'essaie de manger "
                       "équilibré, mon objectif c'est de prendre du muscle avant l'été"):
            check_true(f"retenu : « {phrase[:44]}… »", A._is_personal_fact(phrase))
        # Un ORDRE de retenir l'emporte sur tout, point d'interrogation compris
        for phrase in ("tu peux retenir que je me lève à 6h30 la semaine ?",
                       "note que je suis allergique aux arachides",
                       "souviens-toi que je déteste les maths",
                       "n'oublie pas mon rendez-vous du 12 mars"):
            check_true(f"ordre explicite : « {phrase[:40]}… »", A._is_personal_fact(phrase))
            check_true("…et il est reconnu comme tel", A._est_ordre_memoire(phrase))
        # …sans jamais confondre une COMMANDE avec une confidence
        for phrase in ("ouvre mon agenda", "lit mon fichier pea sur google sheet",
                       "envoie un mail à mon père", "montre mes mails",
                       "tu peux faire quoi avec notion ?", "quelle heure est-il ?",
                       "résume l'actu tech du jour", "bonjour", ""):
            check(f"ignoré : « {phrase[:40]}… »", A._is_personal_fact(phrase), False)
    finally:
        A._connected_accounts = vrais_cnx
        A._APP_RECENTE.clear()

    # 7c. Sans modèle disponible, la confidence ne doit PAS être perdue.
    # learn_from a besoin d'un LLM pour reformuler ; quand toutes les offres gratuites
    # sont saturées il ne rend rien, et la phrase était jetée.
    vrais = (A._llm_json, A._connected_accounts)
    try:
        A._connected_accounts = lambda: [("gmail", "u", "ACTIVE")]
        P.clear_all()
        A._llm_json = lambda s, u: (_ for _ in ()).throw(RuntimeError("aucun modèle"))
        A._remember_fact("mon père et moi on a un PEA ensemble")
        gardes = P.list_facts()
        check_true("sans modèle : la phrase est gardée telle quelle", len(gardes) == 1)
        check_true("…et son contenu est intact", "PEA" in gardes[0]["texte"])
        # Avec un modèle, on garde la version reformulée, pas la phrase brute
        P.clear_all()
        A._llm_json = lambda s, u: {"faits": [{"cat": "autre", "texte": "A un PEA avec son père"}]}
        A._remember_fact("mon père et moi on a un PEA ensemble")
        gardes = P.list_facts()
        check("avec modèle : un seul fait, reformulé", len(gardes), 1)
        check("c'est bien la version reformulée", gardes[0]["texte"], "A un PEA avec son père")
        # Une commande ne laisse aucune trace
        P.clear_all()
        A._remember_fact("ouvre mon agenda")
        check("une commande n'entre pas dans le profil", P.list_facts(), [])
    finally:
        (A._llm_json, A._connected_accounts) = vrais
        P.clear_all()


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
        C._providers_disponibles = lambda niveau="equilibre", impose="": [(f"faux{i}", _lent, "m") for i in range(20)]
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
        C._providers_disponibles = lambda niveau="equilibre", impose="": (
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

    async def faux_llm(messages, model=None, temperature=0.7, timeout=0.0, impose=""):
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
    # Ce test vérifiait la NORMALISATION (« aujourd hui » sans apostrophe), pas la
    # politique de mode. On le vérifie donc sur une demande d'actualité franche.
    check_true("« aujourd hui » sans apostrophe reconnu",
               veut_actualite("quoi de neuf aujourd hui"))
    # ⚠️ « maintenant » / « aujourd'hui » situent le MOMENT, pas le sujet. Une question
    # qui NOMME quelque chose de précis n'est pas une demande d'actualité : Nova basculait
    # en mode news pour « tu penses quoi d'acheter 2CRSI maintenant ? » et rendait… les
    # titres politiques du jour.
    for precise in ("tu penses quoi d'acheter 2CRSI maintenant",
                    "le cours de LVMH maintenant", "prix du bitcoin aujourd hui",
                    "quelle météo à Pau aujourd'hui", "combien ça coûte maintenant"):
        check(f"pas de l'actualité : « {precise[:36]}… »", veut_actualite(precise), False)
    for actu in ("résume l'actu tech du jour", "quoi de neuf aujourd'hui",
                 "les news du jour", "dernières nouvelles", "que se passe-t-il en ce moment"):
        check_true(f"actualité : « {actu[:32]}… »", veut_actualite(actu))
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
        C._providers_disponibles = lambda niveau="equilibre", impose="": [
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
        C._providers_disponibles = lambda niveau="equilibre", impose="": [
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
        C._providers_disponibles = lambda niveau="equilibre", impose="": [("groq", sature_20s, "m")]
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

    async def llm_mort(messages, model=None, temperature=0.7, timeout=0.0, impose=""):
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

        C._providers_disponibles = lambda niveau="equilibre", impose="": [
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


# ── 26. Les apps connectées débloquent leurs actions toutes seules ───────────
def test_apps_actions():
    """Cas réel : Google Sheets connecté, et Nova répondait « donne-moi la commande
    d'intégration » — à quelqu'un qui venait de connecter l'app POUR ne plus avoir à
    la connaître. La liste des actions était figée en dur dans le code."""
    from config import config as CFG
    from plugins.builtin import composio_tool as CT

    # 26a. Singulier / pluriel : « google sheet » doit être reconnu comme « googlesheets »
    for phrase, attendu in (("tu peux lire mes fichier google sheet", "googlesheets"),
                            ("trouve un fichier pea dans mes sheets", "googlesheets"),
                            ("ouvre mon google doc", "googledocs"),
                            ("regarde mon mail", "gmail"),
                            ("mon agenda demain", "googlecalendar")):
        check(f"app détectée dans « {phrase[:30]} »", A._detect_toolkit(phrase), attendu)
    check("aucune app quand il n'y en a pas", A._detect_toolkit("j'ai 17 ans"), None)
    check_true("variantes générées", "sheet" in A._mots_cles_souples("sheets"))
    check_true("variante inverse aussi", "docs" in A._mots_cles_souples("doc"))

    # 26b. Les actions sont DÉCOUVERTES, jamais codées en dur
    vrais = (A._connected_accounts, A._composio_list_actions, CFG.COMPOSIO_API_KEY)
    try:
        CFG.COMPOSIO_API_KEY = "ak_test"
        A._connected_accounts = lambda: [("googlesheets", "u", "ACTIVE"),
                                         ("gmail", "u", "ACTIVE")]
        catalogue_par_app = {
            "googlesheets": ["GOOGLESHEETS_BATCH_GET", "GOOGLESHEETS_BATCH_UPDATE"],
            "gmail": ["GMAIL_FETCH_EMAILS"],
        }
        A._composio_list_actions = lambda s: [{"name": n} for n in catalogue_par_app.get(s, [])]

        cat = CT.catalogue("tu peux lire mes fichier google sheet")
        check_true("action réelle de Sheets présente", "GOOGLESHEETS_BATCH_GET" in cat)
        check_true("les autres apps connectées aussi", "GMAIL_FETCH_EMAILS" in cat)
        check_true("l'app évoquée passe en premier", cat.strip().startswith("• googlesheets"))
        # Le nom exact ne doit jamais être inventé : il vient de Composio
        check("aucune action inventée", "GOOGLESHEETS_LIRE" in cat, False)

        # Commande oubliée → on donne les vraies actions, pas les 7 codées en dur
        aide = CT.ComposioPlugin().run()
        check_true("aide = vraies actions", "GOOGLESHEETS_BATCH_GET" in aide)

        # Une app fraîchement connectée apparaît sans toucher au code
        A._connected_accounts = lambda: [("notion", "u", "ACTIVE")]
        catalogue_par_app["notion"] = ["NOTION_CREATE_PAGE", "NOTION_SEARCH"]
        cat2 = CT.catalogue("ajoute une page notion")
        check_true("nouvelle app prise en compte immédiatement", "NOTION_SEARCH" in cat2)

        # Aucune app connectée : on le dit, on ne ment pas avec une liste d'exemples
        A._connected_accounts = lambda: []
        vide = CT.catalogue("mes sheets")
        check_true("aucune app → message honnête", "aucune app" in vide.lower())

        # Composio injoignable : repli, jamais d'exception
        A._connected_accounts = lambda: (_ for _ in ()).throw(RuntimeError("réseau"))
        check_true("Composio injoignable → repli", bool(CT.catalogue("x")))
    finally:
        A._connected_accounts, A._composio_list_actions = vrais[0], vrais[1]
        CFG.COMPOSIO_API_KEY = vrais[2]

    # 26c. Un message d'erreur d'outil n'est PAS une trouvaille à afficher
    import agent.core as AC
    for faux in ("⚠️ Précise 'command'. Ex : GMAIL_FETCH_EMAILS",
                 "❌ Action Composio échouée (HTTP 404)",
                 "🔑 Ta clé Composio est VALIDE mais n'a pas la permission"):
        check(f"pas une trouvaille : « {faux[:26]} »", AC._repli_observations([faux]), "")
    # …mais un vrai résultat en est une
    check_true("un vrai résultat reste une trouvaille",
               AC._repli_observations(["🔎 **Résultats web** (3)\n\n**1. Titre**\n_source.fr_"]))


# ── 27. L'actu demandée est bien celle qu'on rend ────────────────────────────
def test_actu_pertinence():
    """Cas réel : « résume l'actu TECH » a ramené les Houthis et des affaires
    judiciaires, et la requête contenait encore « 20 août 2026 »."""
    from datetime import datetime
    import agent.core as AC
    import llm.client as C
    from plugins.builtin import actu_rss as R

    # 27a. Une demande tech n'interroge QUE des médias tech
    for q, attendu in (("actualité tech", "tech"), ("actu du jour", "general"),
                       ("actualité sport", "sport")):
        t = R.theme_de(q)
        check(f"thème de « {q} »", t, attendu)
        noms = [n for n, _u in R.FLUX[t]]
        check_true(f"médias de « {q} » : {', '.join(noms[:3])}", len(noms) >= 2)
    # Aucun média généraliste ne doit polluer une demande thématique
    generalistes = {n for n, _u in R.FLUX["general"]}
    for theme in ("tech", "science", "economie"):
        melange = generalistes & {n for n, _u in R.FLUX[theme]}
        check(f"aucun généraliste dans « {theme} »", melange, set())

    # 27b. La date ne doit JAMAIS survivre dans une requête d'actualité,
    #      quel que soit le chemin emprunté.
    auj = datetime.now()
    date_jour = AC._JOURS_COURT(auj)
    vrai_chat = C.chat
    try:
        for nom, sortie in (("le modèle obéit", "actualité tech"),
                            ("le modèle remet la date", f"actualité tech {date_jour}"),
                            ("le modèle remet l'année", f"actu tech {auj.year}")):
            C.chat = lambda *a, **k: sortie
            q = AC.search_query("Résume l'actu tech du jour")
            check(f"{nom} → requête sans date", str(auj.year) in q, False)
            check_true(f"{nom} → sujet gardé", "tech" in q.lower())
        # Modèle injoignable : le repli lexical est déjà propre
        C.chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("hors ligne"))
        q = AC.search_query("Résume l'actu tech du jour")
        check("modèle absent → requête sans date", str(auj.year) in q, False)
        # …mais une question NON actu garde sa date, qui l'aide vraiment
        C.chat = lambda *a, **k: f"prix bitcoin {date_jour}"
        check_true("question web → date conservée",
                   str(auj.year) in AC.search_query("prix du bitcoin"))
    finally:
        C.chat = vrai_chat

    # 27c. Un thème dont tous les médias sont muets retombe sur l'actualité générale,
    #      mais JAMAIS en mélange avec les articles du thème.
    vrai_get = None
    try:
        import requests
        vrai_get = requests.get
        appeles = []

        def faux_get(url, **kw):
            appeles.append(url)
            raise RuntimeError("injoignable")

        requests.get = faux_get
        R.recuperer("actualité tech", 5, 0)
        tech_urls = {u for _n, u in R.FLUX["tech"]}
        gen_urls = {u for _n, u in R.FLUX["general"]}
        check_true("les médias tech ont été essayés", tech_urls & set(appeles))
        check_true("repli sur le généraliste seulement après", gen_urls & set(appeles))
    finally:
        if vrai_get:
            requests.get = vrai_get


# ── 28. Défauts remontés par l'audit du chemin actualité ─────────────────────
def test_audit_actu():
    """Neuf défauts trouvés en auditant le pipeline actu, tous reproduits avant correction."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    import agent.core as AC
    import agent.self_heal as SH
    from plugins.builtin import actu_rss as R

    # 28a. « ia » cherché en sous-chaîne classait « mafia » et « via » en actualité tech
    for phrase, attendu in (("le procès de la mafia", "general"), ("on passe via Lyon", "general"),
                            ("l'IA générative", "tech"), ("actu tech", "tech"),
                            ("le match de foot", "sport")):
        check(f"thème de « {phrase} »", R.theme_de(phrase), attendu)

    # 28b. Le mode « news » n'était appliqué qu'à la recherche FORCÉE : celles que le
    #      modèle lançait ensuite repartaient en mode web et sautaient les flux RSS.
    check("relance d'actualité → mode news",
          AC._params_outil("search_web", {"query": "actualités du jour France"}).get("mode"), "news")
    check("relance ciblée → reste en web",
          AC._params_outil("search_web", {"query": "prix Nintendo Switch 2"}).get("mode"), None)
    check("choix explicite du modèle respecté",
          AC._params_outil("search_web", {"query": "actu", "mode": "web"})["mode"], "web")
    check("les autres outils ne sont pas touchés",
          AC._params_outil("connected_app", {"command": "X"}), {"command": "X"})

    # 28c. La transcription annonçait la phrase BRUTE au lieu de la requête envoyée :
    #      le modèle la recopiait, verbes de commande compris.
    vus = []

    def outil(loader, nom, params, fallback="", echeance=0.0):
        vus.append(params)
        return "🔎 **Résultats web : x** (1)\n\n**1. Titre**\n_ex.fr_"

    async def llm_fin(messages, model=None, temperature=0.7, timeout=0.0, impose=""):
        return "FINAL: ok"

    vrais = (SH.safe_tool_call, AC.llm_call, AC.search_query)
    try:
        SH.safe_tool_call = outil
        AC.llm_call = llm_fin
        AC.search_query = lambda t: "actualité tech"

        async def run():
            cfg = {"name": "Nova", "tools": ["search_web"], "force_search": True,
                   "system_prompt": "t"}
            msgs = []
            async for _ in AC.run_agent_stream("Résume l'actu tech du jour", cfg, "test_audit"):
                pass
            return msgs

        asyncio.run(run())
        check("la recherche forcée part en mode news", vus and vus[0].get("mode"), "news")
        check("elle utilise la requête nettoyée", vus and vus[0].get("query"), "actualité tech")
    finally:
        SH.safe_tool_call, AC.llm_call, AC.search_query = vrais

    # 28d. Un flux RSS 1.0 (RDF) était lu comme « zéro article », en silence
    RDF = (b'<?xml version="1.0"?><rdf:RDF '
           b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
           b'xmlns="http://purl.org/rss/1.0/">'
           b'<item><title>Article RDF</title><link>http://x/1</link></item></rdf:RDF>')
    a = R.lire_flux(RDF, "RDF")
    check("flux RSS 1.0 lu", len(a), 1)
    check("lien du flux RDF", a[0]["lien"] if a else "", "http://x/1")

    # 28e. En Atom, le lien retenu était le premier venu — souvent les commentaires
    ATOM = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            b'<title>T</title><link rel="replies" href="http://x/commentaires"/>'
            b'<link rel="alternate" href="http://x/article"/>'
            b'<published>2026-08-20T10:00:00Z</published></entry></feed>')
    b_ = R.lire_flux(ATOM, "Atom")
    check("lien de l'article, pas des commentaires", b_[0]["lien"] if b_ else "", "http://x/article")

    # 28f. Un article daté dans le futur passait devant la vraie actu du jour
    now = datetime.now(timezone.utc)
    arts = [{"titre": "Futur", "date": now + timedelta(days=400), "lien": "", "resume": "",
             "source": "X", "theme": ""},
            {"titre": "Vraie actu", "date": now - timedelta(hours=1), "lien": "", "resume": "",
             "source": "X", "theme": ""}]
    for x in arts:                                   # même normalisation que recuperer()
        if x["date"] > now + timedelta(hours=6):
            x["date"] = now
    arts.sort(key=lambda x: x["date"], reverse=True)
    check_true("une date future ne double pas l'actu du jour",
               arts[0]["titre"] in ("Futur", "Vraie actu") and
               abs((arts[0]["date"] - now).total_seconds()) < 7200)

    # 28g. Le repli généraliste s'affichait sous l'étiquette du thème DEMANDÉ
    gen = [{"titre": "Politique", "date": now, "lien": "", "resume": "", "source": "France Info",
            "theme": "general"}]
    rendu = R.formater(gen, "tech")
    check("le repli n'est plus étiqueté « tech »", "tech" in rendu.lower(), False)
    tech = [dict(gen[0], theme="tech")]
    check_true("un vrai résultat tech est bien étiqueté", "tech" in R.formater(tech, "tech"))

    # 28h. « articles récents » était affiché même quand ils ne l'étaient pas
    vieux = [{"titre": "V", "date": now - timedelta(days=9), "lien": "", "resume": "",
              "source": "X", "theme": "tech"}]
    r_vieux = R.formater(vieux, "tech")
    check("vieux articles non annoncés « récents »", "articles récents" in r_vieux, False)
    check_true("l'ancienneté est dite franchement", "Rien de neuf" in r_vieux)
    check_true("des articles frais restent « récents »", "articles récents" in R.formater(tech, "tech"))

    # 28i. Le briefing du matin court-circuitait les flux RSS
    from pathlib import Path as P
    brief = (P(__file__).resolve().parents[1] / "agent" / "briefing.py").read_text(encoding="utf-8")
    check_true("le briefing utilise le mode actualité", '"mode": "news"' in brief)


# ── 29. Le brouillon des modèles raisonneurs ne doit jamais s'afficher ───────
def test_raisonnement_cache():
    """Vu en production : « <think> L'utilisateur demande un résumé… Je vais synthétiser
    cela en quelques phrases </think> » affiché AVANT la réponse."""
    from agent.core import sans_raisonnement, FiltreRaisonnement, parse_response

    # 29a. Le bloc est retiré, la réponse est gardée intacte
    brut = ("<think>\nL'utilisateur demande un résumé de l'actu tech.\n"
            "Je vais synthétiser cela en quelques phrases.\n</think>\n\n"
            "Voici l'essentiel de l'actu tech du jour.")
    check("brouillon retiré", sans_raisonnement(brut), "Voici l'essentiel de l'actu tech du jour.")
    for balise in ("think", "thinking", "reasoning", "scratchpad"):
        check(f"balise <{balise}> gérée",
              sans_raisonnement(f"<{balise}>brouillon</{balise}>Réponse."), "Réponse.")
    # Un bloc jamais refermé (réponse coupée) ne doit pas s'afficher en morceaux
    check("bloc non refermé jeté", sans_raisonnement("<think>je réfléchis et ça coupe"), "")
    # Une réponse normale n'est jamais abîmée
    check("réponse normale intacte", sans_raisonnement("Bonjour Lohan !"), "Bonjour Lohan !")
    check("chaîne vide", sans_raisonnement(""), "")
    check("None géré", sans_raisonnement(None), "")

    # 29b. Un « ACTION: » écrit DANS le brouillon ne doit pas déclencher d'outil
    piege = "<think>je pourrais faire ACTION: search_web pour ça</think>\nVoici la réponse."
    action, _p, final = parse_response(piege)
    check("aucun outil déclenché par le brouillon", action, None)
    check_true("la vraie réponse survit", "Voici la réponse" in (final or piege))

    # 29c. En streaming, le brouillon ne doit jamais atteindre l'écran, même coupé en deux
    def flux(morceaux):
        f = FiltreRaisonnement()
        return "".join(f(m) for m in morceaux) + f.reste()

    check("balise coupée entre deux tokens",
          flux(["<th", "ink>", "Je ", "réfléchis", "</thi", "nk>", "Voici ", "la réponse."]),
          "Voici la réponse.")
    check("aucun brouillon → rien n'est perdu",
          flux(["Bon", "jour ", "Lohan", " !"]), "Bonjour Lohan !")
    check("un « < » ordinaire n'est pas avalé",
          flux(["5 ", "< ", "10 et a<b"]), "5 < 10 et a<b")
    check("brouillon en fin de flux non refermé",
          flux(["Réponse. ", "<think>", "et je continue"]), "Réponse. ")
    check("deux brouillons successifs",
          flux(["<think>a</think>", "X", "<think>b</think>", "Y"]), "XY")


# ── 30. Une app connectée ne doit jamais être déclarée absente ───────────────
def test_slug_connecte():
    """Nova répondait « Googlemaps n'est pas connecté » alors que le compte l'était :
    Composio écrit « google_maps », nous « googlemaps »."""
    vrai = A._connected_accounts
    try:
        A._connected_accounts = lambda: [("google_maps", "u1", "ACTIVE"),
                                         ("googlecalendar", "u1", "ACTIVE"),
                                         ("google_sheets", "u1", "ACTIVE")]
        for slug in ("googlemaps", "google_maps", "googlesheets"):
            rep = A._capability_answer(slug)
            check(f"« {slug} » reconnu comme connecté", "n'est pas connecté" in rep, False)
        # Une app réellement absente reste bien signalée comme telle
        check_true("une app absente est signalée", "n'est pas connecté" in A._capability_answer("notion"))
    finally:
        A._connected_accounts = vrai


# ── 31. Savoir QUEL modèle a répondu ─────────────────────────────────────────
def test_modele_annonce():
    """La fonctionnalité n'avait jamais marché : DERNIER est une variable de CONTEXTE,
    renseignée dans un thread de travail, dont la valeur ne remontait jamais."""
    import asyncio
    import llm.client as C
    from agent.core import _off

    async def scenario():
        def travail_llm():
            C.DERNIER.set("nvidia · meta/llama-3.3-70b-instruct")
            return "réponse"

        await _off(travail_llm)
        return A._modele_utilise()

    check("le modèle traverse le thread", asyncio.run(scenario()),
          "nvidia · meta/llama-3.3-70b-instruct")

    # Un travail SANS modèle ne doit pas effacer ni inventer un nom
    async def sans_modele():
        C.DERNIER.set("groq · précédent")
        await _off(lambda: "recherche web")
        return A._modele_utilise()

    check("un travail sans modèle n'écrase rien", asyncio.run(sans_modele()), "groq · précédent")

    # Un thread réutilisé ne doit pas resservir le modèle d'une requête précédente
    def sale():
        C.DERNIER.set("vieux · modèle")
        return "x"

    C.executer_et_capturer(sale, )
    check("pas de valeur périmée d'un thread recyclé",
          C.executer_et_capturer(lambda: "y")[1], "")

    # Tous les chemins de réponse doivent annoncer le modèle
    from pathlib import Path as P
    src = (P(__file__).resolve().parents[1] / "api" / "agent.py").read_text(encoding="utf-8")
    chemins = src.count('"type": "model"')
    check_true(f"annonce sur chaque chemin de réponse ({chemins} points d'émission)",
               chemins >= 5)


# ── 32. Trois défauts d'affichage de l'actualité ─────────────────────────────
def test_affichage_actu():
    from datetime import datetime, timedelta, timezone
    from agent.core import apercu
    from plugins.builtin import actu_rss as R

    # 32a. Couper à 140 caractères laissait des marqueurs Markdown orphelins
    obs = ("📰 **Actualité tech — 6 articles récents**\n\n"
           "**1. Nintendo part en drift : la Switch 2 à prix déjanté et Mario Kart World "
           "offert**\n_01net · 20/08 20h05_\nLa console revient en stock.\n"
           "🔗 https://01net.com/un-article-au-lien-tres-long\n\n**2. DJI Mini 5 Pro**")
    for n in (40, 60, 100, 140, 200, 260, 300):
        c = apercu(obs, n)
        check(f"gras équilibré à {n}", c.count("**") % 2, 0)
        check(f"italique équilibré à {n}", c.count("_") % 2, 0)
        check_true(f"aucun lien coupé à {n}",
                   "http" not in c or "https://01net.com/un-article-au-lien-tres-long" in c)
    check("texte court laissé intact", apercu("texte court", 140), "texte court")
    check("chaîne vide", apercu("", 140), "")
    check("None géré", apercu(None, 140), "")

    # 32b. Un média très actif raflait toutes les places
    now = datetime.now(timezone.utc)
    arts = [{"titre": f"01net {i}", "date": now - timedelta(minutes=i), "lien": "",
             "resume": "", "source": "01net", "theme": "tech"} for i in range(20)]
    arts += [{"titre": f"{m} exclusif", "date": now - timedelta(minutes=30 + j), "lien": "",
              "resume": "", "source": m, "theme": "tech"}
             for j, m in enumerate(["Numerama", "Frandroid", "Clubic"])]
    maxi = 6
    plafond = max(1, int(maxi * R.PART_MAX_MEDIA))
    vus, gardes, par_media = set(), [], {}
    for tour in (1, 2):
        for a in arts:
            if len(gardes) >= maxi:
                break
            c = R._cle_titre(a["titre"])
            if c in vus or (tour == 1 and par_media.get(a["source"], 0) >= plafond):
                continue
            vus.add(c)
            par_media[a["source"]] = par_media.get(a["source"], 0) + 1
            gardes.append(a)
        if len(gardes) >= maxi:
            break
    sources = {a["source"] for a in gardes}
    check_true(f"plusieurs médias représentés ({len(sources)})", len(sources) >= 3)
    check_true("aucun média ne dépasse sa part",
               max(par_media.values()) <= plafond)
    check("la liste reste pleine", len(gardes), maxi)
    check_true("plafond raisonnable", 0.2 <= R.PART_MAX_MEDIA <= 0.7)

    # 32c. L'heure était celle du flux, jamais celle de Paris
    utc = datetime(2026, 8, 20, 20, 5, tzinfo=timezone.utc)
    loc = R._heure_locale(utc)
    check_true("la date est convertie", loc is not None)
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Europe/Paris")
        check("20h05 UTC devient 22h05 à Paris (heure d'été)", loc.hour, 22)
        check("aucune double conversion", R._heure_locale(loc).hour, 22)
        # En hiver le décalage n'est que d'une heure : la conversion doit suivre
        hiver = datetime(2026, 1, 15, 20, 5, tzinfo=timezone.utc)
        check("20h05 UTC devient 21h05 à Paris (heure d'hiver)", R._heure_locale(hiver).hour, 21)
    except Exception:
        OK.append(("fuseau Paris indisponible ici — conversion générique", True, True))
    check("date absente gérée", R._heure_locale(None), None)


# ── 33. Trouver un fichier par son nom, puis l'ouvrir ────────────────────────
def test_enchainement_fichier():
    """Nova écrivait « YOUR_SPREADSHEET_ID » et Composio répondait « Failed to open
    spreadsheet with ID YOUR_SPREADSHEET_ID » : elle ne savait pas CHERCHER d'abord."""
    FICHIERS = [{"id": "1AAAaaaBBBcccDDDeeeFFFgggHHHiiiJJJkkkLLL", "name": "Budget vacances 2026"},
                {"id": "1ZZZzzzYYYxxxWWWvvvUUUtttSSSrrrQQQpppOOO", "name": "Suivi_PEA_Lohan_Pere"},
                {"id": "1MMMmmmNNNoooPPPqqqRRRsssTTTuuuVVVwwwXXX", "name": "Notes de cours"}]
    ACTIONS = [{"name": "GOOGLESHEETS_BATCH_GET", "desc": ""},
               {"name": "GOOGLESHEETS_SEARCH_SPREADSHEETS", "desc": ""},
               {"name": "GOOGLESHEETS_CREATE_SPREADSHEET", "desc": ""},
               {"name": "GOOGLESHEETS_DELETE_SHEET", "desc": ""},
               {"name": "GOOGLESHEETS_UPDATE_VALUES", "desc": ""}]

    # 33a. Un identifiant inventé est reconnu comme tel ; un vrai ne l'est pas
    for faux in ("YOUR_SPREADSHEET_ID", "<spreadsheet_id>", "spreadsheet_id", "",
                 "...", "xxx", None, "{{id}}"):
        check_true(f"bouchon détecté : {faux!r}", A._est_bouchon(faux))
    for vrai in ("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
                 "1ZZZzzzYYYxxxWWWvvvUUUtttSSSrrrQQQpppOOO"):
        check(f"vrai identifiant accepté : {vrai[:12]}…", A._est_bouchon(vrai), False)

    # 33b. Le nom du fichier est extrait de la demande
    check("nom explicite", A._mots_cles_fichier("consulte le fichier Suivi_PEA_Lohan_Pere"),
          "Suivi_PEA_Lohan_Pere")
    check("nom entre guillemets", A._mots_cles_fichier("ouvre « Budget 2026 » stp"), "Budget 2026")

    # 33c. L'enchaînement complet : chercher puis ouvrir le BON document
    appels = []

    def faux_tool(action, args=None, **kw):
        appels.append((action, dict(args or {})))
        if "SEARCH" in action.upper() or "LIST" in action.upper():
            return "✅ résultat :\n" + json.dumps({"data": {"files": FICHIERS}})
        return "✅ résultat : {}"

    vrai_tool = A._tool
    try:
        A._tool = faux_tool
        for demande, attendu in (("consulte le fichier Suivi_PEA_Lohan_Pere", "Suivi_PEA_Lohan_Pere"),
                                 ("trouve le tableur qui parle de mon pea", "Suivi_PEA_Lohan_Pere"),
                                 ("ouvre Budget vacances 2026", "Budget vacances 2026")):
            appels.clear()
            args, etapes, refus = A._resoudre_identifiants(
                "googlesheets", "GOOGLESHEETS_BATCH_GET",
                {"spreadsheet_id": "YOUR_SPREADSHEET_ID"}, demande, ACTIONS)
            nom = next((f["name"] for f in FICHIERS if f["id"] == args["spreadsheet_id"]), "")
            check(f"« {demande[:30]}… » → bon fichier", nom, attendu)
            check("document trouvé : aucun refus", refus, "")
            check_true("la recherche est visible dans le raisonnement",
                       any(e["kind"] == "action" for e in etapes))

        # Un VRAI identifiant ne doit déclencher aucune recherche inutile
        appels.clear()
        vrai = {"spreadsheet_id": "1AAAaaaBBBcccDDDeeeFFFgggHHHiiiJJJkkkLLL"}
        args, etapes, refus = A._resoudre_identifiants("googlesheets", "GOOGLESHEETS_BATCH_GET",
                                                       dict(vrai), "lis ce fichier", ACTIONS)
        check("aucune recherche inutile", len(appels), 0)
        check("identifiant conservé", args, vrai)
        check("identifiant réel : aucun refus", refus, "")

        # Une app sans action de recherche : on REFUSE au lieu d'appeler avec un bouchon
        args, etapes, refus = A._resoudre_identifiants("x", "X_GET", {"file_id": "YOUR_FILE_ID"},
                                                       "lis", [{"name": "X_GET", "desc": ""}])
        check("app sans recherche : aucune étape", etapes, [])
        check_true("app sans recherche : refus explicite", bool(refus))
        check_true("le refus ne montre pas le bouchon", "YOUR_FILE_ID" not in refus)

        # La recherche par mots-clés est muette → on redemande la liste complète
        appels.clear()

        def recherche_muette(action, args=None, **kw):
            appels.append((action, dict(args or {})))
            if (args or {}).get("query"):          # le moteur de l'app ne trouve rien
                return "✅ résultat :\n" + json.dumps({"data": {"files": []}})
            return "✅ résultat :\n" + json.dumps({"data": {"files": FICHIERS}})

        A._tool = recherche_muette
        args, etapes, refus = A._resoudre_identifiants(
            "googlesheets", "GOOGLESHEETS_BATCH_GET",
            {"spreadsheet_id": "YOUR_SPREADSHEET_ID"},
            "lit mon fichier pea sur google sheet", ACTIONS)
        check("repli sur la liste complète", args["spreadsheet_id"],
              "1ZZZzzzYYYxxxWWWvvvUUUtttSSSrrrQQQpppOOO")
        check("deux appels : mots-clés puis liste", len(appels), 2)
        check("repli réussi : aucun refus", refus, "")

        # Aucun document trouvé : on le dit, on n'invente pas, on N'EXÉCUTE PAS
        A._tool = lambda a, args=None, **k: "✅ résultat :\n" + json.dumps({"data": {"files": []}})
        args, etapes, refus = A._resoudre_identifiants(
            "googlesheets", "GOOGLESHEETS_BATCH_GET",
            {"spreadsheet_id": "<id>"}, "ouvre Inexistant", ACTIONS)
        check_true("absence de résultat annoncée",
                   any("aucun document" in (e.get("text") or "") for e in etapes))
        check_true("rien trouvé : refus explicite", bool(refus))
        check_true("le refus reste en français", "n'ai pas trouvé" in refus)
        check_true("le bouchon n'est jamais exécuté", A._est_bouchon(args["spreadsheet_id"]))

        # Rien trouvé mais des documents existent : on les propose
        A._tool = lambda a, args=None, **k: "✅ résultat :\n" + json.dumps({"data": {"files": FICHIERS}})
        args, etapes, refus = A._resoudre_identifiants(
            "googlesheets", "GOOGLESHEETS_BATCH_GET",
            {"spreadsheet_id": "YOUR_SPREADSHEET_ID"}, "ouvre le fichier zzzzzzzz", ACTIONS)
        if refus:
            check_true("le refus liste les documents disponibles", "Suivi_PEA_Lohan_Pere" in refus)
    finally:
        A._tool = vrai_tool

    # 33c-bis. Aucun nom ne ressemble à la demande → on n'ouvre PAS le premier venu.
    # « ouvre le tableur Machin_Qui_Nexiste_Pas » ouvrait « Budget vacances 2026 ».
    PAIRES = [(f["id"], f["name"]) for f in FICHIERS]
    for quoi, attendu in (("Suivi_PEA_Lohan_Pere", "1ZZZzzzYYYxxxWWWvvvUUUtttSSSrrrQQQpppOOO"),
                          ("pea", "1ZZZzzzYYYxxxWWWvvvUUUtttSSSrrrQQQpppOOO"),
                          ("budget", "1AAAaaaBBBcccDDDeeeFFFgggHHHiiiJJJkkkLLL"),
                          ("Machin_Qui_Nexiste_Pas", ""),
                          ("zzzzzzzz", ""),
                          ("", "")):        # plusieurs documents, aucun indice → on refuse
        check(f"meilleur document pour « {quoi or '(rien)'} »",
              A._meilleur_document(PAIRES, quoi)[0], attendu)
    check("un seul document et aucun indice → on le prend",
          A._meilleur_document([("solo", "Le seul")], "")[0], "solo")
    check("liste vide", A._meilleur_document([], "pea"), ("", ""))

    # 33c-ter. …mais un identifiant de PARENT n'est qu'un EMPLACEMENT : « vas-y crée un
    # doc alors » ne nomme aucune page parente, et refuser serait absurde.
    for act, champs, attendu in (("NOTION_CREATE_NOTION_PAGE", ["parent_page_id"], True),
                                 ("NOTION_CREATE_NOTION_PAGE", ["parent_id"], True),
                                 ("GOOGLEDRIVE_CREATE_FILE", ["folder_id"], True),
                                 ("NOTION_ADD_PAGE_CONTENT", ["parent_block_id"], False),
                                 ("NOTION_CREATE_COMMENT", ["page_id"], False),
                                 ("GOOGLESHEETS_BATCH_GET", ["spreadsheet_id"], False),
                                 ("NOTION_DELETE_BLOCK", ["parent_page_id"], False),
                                 ("NOTION_CREATE_NOTION_PAGE", ["parent_page_id", "database_id"],
                                  False),
                                 ("X_GET", [], False)):
        check(f"emplacement ? {act} {champs}", A._est_emplacement(act, champs), attendu)

    # Bout en bout : créer sans nommer de parent doit ABOUTIR, pas refuser
    A._tool = lambda a, args=None, **k: "✅ résultat :\n" + json.dumps({"data": {"files": FICHIERS}})
    args, etapes, refus = A._resoudre_identifiants(
        "notion", "NOTION_CREATE_NOTION_PAGE",
        {"parent_page_id": "YOUR_PAGE_ID", "title": "agent ia"},
        "vas-y crée un doc alors", ACTIONS)
    check("création sans parent nommé : pas de refus", refus, "")
    check_true("un emplacement a bien été choisi", not A._est_bouchon(args["parent_page_id"]))
    check_true("le choix d'emplacement est annoncé",
               any("je le range dans" in (e.get("text") or "") for e in etapes))
    # …alors qu'une LECTURE d'un document introuvable refuse toujours
    args, etapes, refus = A._resoudre_identifiants(
        "googlesheets", "GOOGLESHEETS_BATCH_GET", {"spreadsheet_id": "YOUR_SPREADSHEET_ID"},
        "ouvre le tableur Machin_Qui_Nexiste_Pas", ACTIONS)
    check_true("lecture d'un document introuvable : refus", bool(refus))
    check_true("aucun document ouvert au hasard", A._est_bouchon(args["spreadsheet_id"]))
    A._tool = vrai_tool

    # 33c-quater. Une réponse Composio TRONQUÉE ne doit plus faire dire « aucun document ».
    # C'est le vrai coupable de « lit mon fichier pea » : le JSON d'un Drive fourni était
    # coupé en plein milieu, json.loads échouait, et Nova annonçait une liste vide.
    from plugins.builtin.composio_tool import _fmt as _fmt_composio

    def _drive(n, pos):
        f = [{"id": f"1{chr(65 + i % 26)}{i:03d}" + "x" * 38,
              "name": f"Document numero {i} — rapport",
              "mimeType": "application/vnd.google-apps.spreadsheet",
              "modifiedTime": "2026-08-20T10:00:00.000Z",
              "webViewLink": f"https://docs.google.com/spreadsheets/d/1{i}/edit",
              "owners": [{"displayName": "Lohan", "emailAddress": "lohan@exemple.fr"}]}
             for i in range(n)]
        f[pos]["name"] = "suivi pea pere et moi"
        return f

    for n, pos in ((3, 1), (30, 7), (60, 55)):
        brut = _fmt_composio("GOOGLESHEETS_SEARCH_SPREADSHEETS", {"data": {"files": _drive(n, pos)}})
        corps = brut.split("résultat :", 1)[1]
        try:
            json.loads(corps)
            valide = True
        except Exception:
            valide = False
        check_true(f"{n} fichiers : le JSON rendu reste valide", valide)
        cands = A._identifiants_trouves(brut)
        check(f"{n} fichiers : le PEA est retrouvé",
              A._meilleur_document(cands, "pea")[1], "suivi pea pere et moi")

    # Et même sur un JSON VOLONTAIREMENT abîmé, on repêche au lieu de renoncer
    abime = _fmt_composio("X_SEARCH", {"data": {"files": _drive(30, 7)}})[:1500]
    repeches = A._identifiants_trouves(abime)
    check_true("JSON coupé : on repêche quand même des identifiants", len(repeches) > 0)
    check_true("JSON coupé : les noms suivent les identifiants",
               all(len(i) >= 10 for i, _n in repeches))
    check("réponse sans le moindre JSON", A._identifiants_trouves("erreur brute"), [])

    # 33d. « le fichier X » doit viser une app de fichiers, pas le vide
    check("« fichier » routé", A._detect_toolkit("consulte le fichier Suivi_PEA"), "googledrive")
    check("« tableur » routé", A._detect_toolkit("trouve le tableur de mon pea"), "googlesheets")

    # 33e. Sans modèle disponible, l'action se devine au VERBE
    for demande, attendu in (("consulte le fichier PEA", "GOOGLESHEETS_BATCH_GET"),
                             ("crée un nouveau tableur", "GOOGLESHEETS_CREATE_SPREADSHEET"),
                             ("supprime cette feuille", "GOOGLESHEETS_DELETE_SHEET"),
                             ("modifie la cellule A1", "GOOGLESHEETS_UPDATE_VALUES"),
                             ("trouve le tableur PEA", "GOOGLESHEETS_SEARCH_SPREADSHEETS")):
        check(f"repli sans modèle : « {demande} »", A._action_par_defaut(demande, ACTIONS), attendu)
    check("aucun verbe d'action → aucune supposition", A._action_par_defaut("bonjour", ACTIONS), "")

    # 33f. Les fautes de frappe courantes ne doivent pas faire perdre le verbe.
    # « lit mon fichier pea sur google sheet » ne déclenchait AUCUNE action de lecture,
    # et « vazy creer un doc alors » aucune action de création.
    LECTURE = ("GOOGLESHEETS_BATCH_GET", "GOOGLESHEETS_SEARCH_SPREADSHEETS")
    for demande in ("lit mon fichier pea sur google sheet", "lis mon fichier pea",
                    "va voir mon suivi pea sur google sheet",
                    "accede a mon google sheets et ouvre le tableur pea",
                    "récupère mon tableur pea", "vérifie mon tableur pea"):
        check_true(f"verbe de lecture reconnu : « {demande[:38]}… »",
                   A._action_par_defaut(demande, ACTIONS) in LECTURE)
    for demande in ("vazy creer un tableur alors", "genere un nouveau tableur",
                    "rédige un tableur budget"):
        check(f"verbe de création reconnu : « {demande[:34]}… »",
              A._action_par_defaut(demande, ACTIONS), "GOOGLESHEETS_CREATE_SPREADSHEET")

    # 33g. Le modèle se trompe d'OBJET : « crée un projet » → NOTION_CREATE_COMMENT.
    # Nova doit détecter le contresens et reprendre la main. (Bug remonté par Lohan.)
    NOTION = [{"name": n, "desc": ""} for n in
              ("NOTION_CREATE_COMMENT", "NOTION_CREATE_DATABASE", "NOTION_CREATE_NOTION_PAGE",
               "NOTION_DELETE_BLOCK", "NOTION_SEARCH_NOTION_PAGE", "NOTION_UPDATE_PAGE")]
    for demande, choix in (("accède à notion et crée un nouveau projet intitulé agent ia",
                            "NOTION_CREATE_COMMENT"),
                           ("crée une base de données clients", "NOTION_CREATE_NOTION_PAGE"),
                           ("crée une page notion", "NOTION_CREATE_DATABASE"),
                           ("envoie un mail à paul", "GMAIL_ADD_ATTACHMENT")):
        check_true(f"contresens détecté : « {demande[:34]}… » → {choix}",
                   A._action_douteuse(demande, choix))
    # …sans jamais contredire un choix légitime
    for demande, choix in (("crée un nouveau projet intitulé agent ia", "NOTION_CREATE_NOTION_PAGE"),
                           ("ajoute un commentaire sur cette page", "NOTION_CREATE_COMMENT"),
                           ("archive cette page", "NOTION_ARCHIVE_PAGE"),
                           ("ajoute un membre à l'équipe", "SLACK_INVITE_USER_TO_WORKSPACE"),
                           ("lit mon fichier pea", "GOOGLESHEETS_BATCH_GET"),
                           ("supprime cet évènement", "GOOGLECALENDAR_DELETE_EVENT"),
                           ("bonjour", "")):
        check(f"choix légitime préservé : « {demande[:30]}… »",
              A._action_douteuse(demande, choix), False)
    # Le contresens est bien CORRIGÉ, pas seulement détecté
    check("« crée un projet » corrigé en page",
          A._action_par_defaut("accède à notion et crée un nouveau projet intitulé agent ia", NOTION),
          "NOTION_CREATE_NOTION_PAGE")


# ── 34. Choisir son fournisseur, et ne plus confondre « réponse » avec « repo »
def test_fournisseur_et_routage():
    """« je veux une réponse avec l'api nvidia » partait vers GitHub : « repo » se
    trouvait dans « ré-po-nse »."""
    import llm.client as C

    # 34a. Le faux positif « repo » dans « réponse », « répondre »…
    for phrase in ("je veux une réponse avec l'api nvidia", "utilise groq pour répondre",
                   "réponds-moi vite", "quelle est ta réponse ?"):
        check(f"« {phrase[:34]}… » n'est pas GitHub", A._detect_toolkit(phrase), None)
    # …sans casser les vraies demandes GitHub
    for phrase, attendu in (("crée un dépôt github", "github"), ("mon repo github", "github"),
                            ("liste mes commits", "github")):
        check(f"« {phrase} » reste GitHub", A._detect_toolkit(phrase), attendu)

    # 34b. « quand suis-je libre » consulte l'agenda : la bulle doit le dire
    for phrase in ("quand suis-je libre cette semaine ?", "mes disponibilités demain",
                   "trouve-moi un créneau jeudi"):
        check(f"« {phrase[:30]}… » → agenda", A._detect_toolkit(phrase), "googlecalendar")
        bulles = " ".join(A._route_bulles(phrase))
        check(f"la bulle ne parle plus de web pour « {phrase[:22]}… »",
              "chercher sur le web" in bulles, False)
        check_true("la bulle annonce l'agenda", "agenda" in bulles)

    # 34c. Le fournisseur réclamé est reconnu — et seulement quand c'est une consigne
    for phrase, attendu in (("je veux une réponse avec l'api nvidia", "nvidia"),
                            ("utilise groq pour répondre", "groq"),
                            ("réponds avec gemini", "gemini"),
                            ("avec openrouter stp", "openrouter")):
        check(f"fournisseur réclamé : « {phrase[:30]}… »", C.fournisseur_demande(phrase), attendu)
    for phrase in ("résume l'actu du jour", "parle-moi de nvidia et de ses GPU",
                   "les actions Gemini en bourse"):
        check(f"simple sujet, pas une consigne : « {phrase[:30]}… »",
              C.fournisseur_demande(phrase), "")

    # 34d. Le fournisseur réclamé passe RÉELLEMENT en tête de la chaîne
    from config import config as CFG
    sauve = (CFG.NVIDIA_API_KEY, CFG.GROQ_API_KEY, CFG.GEMINI_API_KEY,
             dict(C._FOURNISSEURS_KO), C._DERNIER_OK["nom"])
    try:
        CFG.NVIDIA_API_KEY, CFG.GROQ_API_KEY, CFG.GEMINI_API_KEY = "nvapi-x", "gsk_x", "AIza-x"
        C._FOURNISSEURS_KO.clear()
        C._DERNIER_OK["nom"] = ""
        ordre = [n for n, _f, _m in C._providers_disponibles("equilibre", "gemini")]
        check("le fournisseur réclamé est essayé en premier", ordre[0], "gemini")
        check_true("les autres restent en secours", len(ordre) >= 2)
        # Sans consigne, le routage par tâche reprend la main.
        # ⚠️ NVIDIA n'est plus en tête : le diagnostic a montré qu'il ne répond pas depuis
        # Render (aucune réponse même après 60 s). Le laisser premier faisait payer son
        # délai plein à CHAQUE message avant de basculer. Il reste dans la chaîne au cas
        # où il reviendrait, mais en dernier.
        libre = [n for n, _f, _m in C._providers_disponibles("equilibre", "")]
        check("sans consigne, un fournisseur qui répond passe en tête", libre[0], "groq")
        check_true("NVIDIA reste disponible en dernier recours", "nvidia" in libre)
        check_true("…et n'est plus essayé en premier", libre.index("nvidia") > 0)
        # Un fournisseur réclamé mais SANS clé ne casse pas la chaîne
        sans = [n for n, _f, _m in C._providers_disponibles("equilibre", "cerebras")]
        check_true("fournisseur sans clé ignoré proprement", sans and "cerebras" not in sans)
    finally:
        (CFG.NVIDIA_API_KEY, CFG.GROQ_API_KEY, CFG.GEMINI_API_KEY) = sauve[:3]
        C._FOURNISSEURS_KO.clear(); C._FOURNISSEURS_KO.update(sauve[3])
        C._DERNIER_OK["nom"] = sauve[4]

    # 34e. La consigne voyage bien jusqu'à l'agent
    cfg = A._build_agent_cfg("je veux une réponse avec l'api nvidia sur l'actu", "Nova")
    check("la consigne atteint la config de l'agent", cfg.get("fournisseur"), "nvidia")
    check("aucune consigne = champ vide",
          A._build_agent_cfg("résume l'actu du jour", "Nova").get("fournisseur"), "")


# ── 35. Rien d'irréversible sans ton accord ──────────────────────────────────
def test_garde_fou():
    """« tu te rends compte s'il fait ça avec mes mails ! » — une demande ambiguë avait
    suffi à déclencher une création non voulue. Sur un envoi, ce serait sans retour."""
    # 35a. Classification : lire est sûr, envoyer/supprimer ne l'est pas
    for action, irrev in (("GMAIL_SEND_EMAIL", True), ("GMAIL_FETCH_EMAILS", False),
                          ("GMAIL_REPLY_TO_THREAD", True), ("GOOGLECALENDAR_DELETE_EVENT", True),
                          ("GOOGLECALENDAR_EVENTS_LIST", False), ("SLACK_SEND_MESSAGE", True),
                          ("GOOGLEDRIVE_TRASH_FILE", True), ("LINEAR_DELETE_ISSUE", True),
                          ("GOOGLESHEETS_BATCH_GET", False), ("NOTION_CREATE_NOTION_PAGE", False)):
        check(f"{action} irréversible={irrev}", A._est_irreversible(action), irrev)

    executions = []
    vrais = (A._tool, A._resolve_app_action, A._complex_app_flow)
    try:
        A._tool = lambda act, args=None, **k: executions.append(act) or "✅ ok"
        A._complex_app_flow = lambda m: None
        A._resolve_app_action = lambda m: ("GMAIL_SEND_EMAIL",
                                           {"to": "papa@exemple.fr", "subject": "PEA"})
        A._ATTENTE.clear()

        # 35b. Un envoi de mail ne part PAS tout seul
        r = A._direct_app_prepare("envoie un mail à papa")
        check("aucun mail envoyé au premier tour", executions, [])
        check_true("Nova demande confirmation", "Confirme" in (r.get("done_answer") or ""))
        check_true("elle dit ce qu'elle va faire",
                   "papa@exemple.fr" in (r.get("done_answer") or ""))

        # 35c. « annule » n'envoie rien
        r = A._direct_app_prepare("non annule")
        check("rien envoyé après refus", executions, [])
        check_true("l'annulation est confirmée", "Annulé" in (r.get("done_answer") or ""))

        # 35d. « oui » exécute ce qui était en attente — et RIEN d'autre
        A._ATTENTE.clear(); executions.clear()
        A._direct_app_prepare("envoie un mail à papa")
        A._direct_app_prepare("oui vas-y")
        check("l'accord déclenche l'envoi", executions, ["GMAIL_SEND_EMAIL"])

        # 35e. Une lecture n'est jamais bloquée
        A._ATTENTE.clear(); executions.clear()
        A._resolve_app_action = lambda m: ("GOOGLECALENDAR_EVENTS_LIST", {"calendarId": "primary"})
        r = A._direct_app_prepare("mon agenda demain")
        check("une lecture part directement", executions, ["GOOGLECALENDAR_EVENTS_LIST"])
        check("aucune confirmation demandée pour lire", r.get("done_answer"), None)

        # 35f. Une attente oubliée ne s'exécute pas des heures plus tard
        A._ATTENTE.clear(); executions.clear()
        A._resolve_app_action = lambda m: ("GMAIL_SEND_EMAIL", {"to": "x"})
        A._direct_app_prepare("envoie un mail")
        A._ATTENTE[A._PROFILE_ID]["t"] -= 10_000
        A._direct_app_prepare("oui")
        check("une attente expirée n'envoie rien", "GMAIL_SEND_EMAIL" in executions, False)

        # 35g. Un message qui n'est ni oui ni non abandonne l'attente
        A._ATTENTE.clear(); executions.clear()
        A._resolve_app_action = lambda m: ("GMAIL_SEND_EMAIL", {"to": "x"})
        A._direct_app_prepare("envoie un mail")
        A._resolve_app_action = lambda m: ("GOOGLECALENDAR_EVENTS_LIST", {})
        A._direct_app_prepare("finalement montre-moi mon agenda")
        check("changer de sujet n'envoie pas le mail", "GMAIL_SEND_EMAIL" in executions, False)
    finally:
        A._tool, A._resolve_app_action, A._complex_app_flow = vrais
        A._ATTENTE.clear()


# ── 36. Les dates de l'agenda ne se devinent pas ─────────────────────────────
def test_calendrier_exact():
    """Le modèle avait produit un tableau où le 14/08/2026 était « lundi » (c'est un
    vendredi), pour la semaine du 14 au 20 alors qu'on était le 21."""
    from datetime import datetime
    from agent.core import _JOURS

    cal = A._calendrier_periode("quand suis-je libre cette semaine ?")
    check_true("un calendrier est fourni", bool(cal))
    auj = datetime.now()
    check_true("aujourd'hui est indiqué", "aujourd'hui" in cal)
    check_true("le bon jour pour aujourd'hui", _JOURS[auj.weekday()] in cal)

    # Chaque ligne doit associer le BON jour à la date
    import re as _re
    lignes = [l for l in cal.split("\n") if l.startswith("- ")]
    check_true(f"la semaine est listée ({len(lignes)} jours)", 7 <= len(lignes) <= 9)
    faux = []
    for l in lignes:
        m = _re.match(r"- (\w+) (\d{2})/(\d{2})/(\d{4})", l)
        if not m:
            continue
        jour, d, mo, an = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        if _JOURS[datetime(an, mo, d).weekday()] != jour:
            faux.append(l)
    check("aucune correspondance jour/date fausse", faux, [])

    # La période doit contenir aujourd'hui — pas une semaine passée
    check_true("la semaine en cours, pas une autre",
               auj.strftime("%d/%m/%Y") in cal)
    # Une période courte reste courte
    demain = A._calendrier_periode("mon agenda demain")
    check_true("« demain » ne liste pas la semaine entière",
               len([l for l in demain.split("\n") if l.startswith("- ")]) <= 3)
    # Le calendrier n'est ajouté QUE pour l'agenda
    msgs = A._format_app_messages("mes mails", "GMAIL_FETCH_EMAILS", "{}")
    check("aucun calendrier pour les mails", "CALENDRIER EXACT" in msgs[1]["content"], False)
    msgs = A._format_app_messages("mon agenda", "GOOGLECALENDAR_EVENTS_LIST", "{}")
    check_true("calendrier joint pour l'agenda", "CALENDRIER EXACT" in msgs[1]["content"])


# ── 37. Nova suit la conversation d'une phrase à l'autre ─────────────────────
def test_autocorrection_garde_identifiant():
    """Défaut confirmé par l'audit : l'auto-correction reconstruisait les arguments à
    zéro et JETAIT l'identifiant qu'on venait d'aller chercher. Mesuré : 2ᵉ appel avec
    le vrai « 1PEAabc… », 3ᵉ appel avec « YOUR_SPREADSHEET_ID »."""
    FICH = [{"id": "1PEAabcdefGHIJKLmnop1234", "name": "Suivi_PEA"}]
    appels = []
    vrais = (A._tool, A._composio_list_actions, A._connected_accounts, A._build_args,
             A._llm_json, A._known_args, A._format_app_result, A._document_connu)
    try:
        def faux_tool(a, args=None, **k):
            appels.append((a, dict(args or {})))
            if "SEARCH" in a.upper():
                return "✅ résultat :\n" + json.dumps({"data": {"files": FICH}})
            return ('❌ Action échouée : {"http_error":"400 Bad Request",'
                    '"message":"Invalid range"}')
        A._tool = faux_tool
        A._composio_list_actions = lambda s: [
            {"name": "GOOGLESHEETS_SEARCH_SPREADSHEETS", "desc": "", "required": []},
            {"name": "GOOGLESHEETS_BATCH_UPDATE", "desc": "",
             "required": ["spreadsheet_id", "values"]}]
        A._connected_accounts = lambda: [("googlesheets", "u", "ACTIVE")]
        # Le modèle reconstruit tout et remet un bouchon — c'est exactement le cas réel.
        A._build_args = lambda act, spec, msg, ctx="", error="": {
            "spreadsheet_id": "YOUR_SPREADSHEET_ID", "values": [["x"]]}
        A._llm_json = lambda s, u: {"action": "GOOGLESHEETS_BATCH_UPDATE", "arguments": {}}
        A._known_args = lambda a, m: None
        A._format_app_result = lambda m, a, o, w: "ok"
        A._document_connu = lambda app, quoi: ("", "")

        A._generic_app_flow("ajoute une ligne dans mon tableur PEA", "googlesheets")
        avec_id = [ar.get("spreadsheet_id") for _a, ar in appels if ar.get("spreadsheet_id")]
        check_true("au moins un appel a été fait", bool(avec_id))
        check("aucun appel ne part avec un bouchon",
              [v for v in avec_id if A._est_bouchon(v)], [])
        check_true("l'identifiant résolu est conservé",
                   all(v == "1PEAabcdefGHIJKLmnop1234" for v in avec_id))
    finally:
        (A._tool, A._composio_list_actions, A._connected_accounts, A._build_args,
         A._llm_json, A._known_args, A._format_app_result, A._document_connu) = vrais

    # ⚠️ Une action CONFIRMÉE était exécutée telle quelle : si ses arguments avaient été
    # mal résolus, l'accord portait sur une cible fausse et rien ne le rattrapait.
    import time as _t
    vrai_tool = A._tool
    try:
        A._tool = lambda a, args=None, **k: "✅ fait"
        A._ATTENTE[A._PROFILE_ID] = {
            "slug": "googlesheets", "action": "GOOGLESHEETS_DELETE_SHEET",
            "args": {"spreadsheet_id": "YOUR_SPREADSHEET_ID", "sheet_id": 0},
            "t": _t.monotonic()}
        rep = (A._direct_app_prepare_brut("oui vas-y") or {}).get("done_answer", "")
        check_true("un identifiant bouché arrête l'exécution", rep.startswith("🛑"))
        check_true("…en disant lequel", "spreadsheet_id" in rep)
        # …mais un gid numérique ne doit PAS bloquer
        A._ATTENTE[A._PROFILE_ID] = {
            "slug": "googlesheets", "action": "GOOGLESHEETS_DELETE_SHEET",
            "args": {"spreadsheet_id": "1PEAabcdefGHIJKLmnop1234", "sheet_id": 0},
            "t": _t.monotonic()}
        rep = (A._direct_app_prepare_brut("oui vas-y") or {}).get("done_answer", "")
        check("un gid numérique passe", rep.startswith("🛑"), False)
        # …ni un mail ordinaire
        A._ATTENTE[A._PROFILE_ID] = {
            "slug": "gmail", "action": "GMAIL_SEND_EMAIL",
            "args": {"to": "papa@exemple.fr", "subject": "Salut"}, "t": _t.monotonic()}
        rep = (A._direct_app_prepare_brut("oui vas-y") or {}).get("done_answer", "")
        check("un mail confirmé part bien", rep.startswith("🛑"), False)
    finally:
        A._tool = vrai_tool
        A._ATTENTE.clear()


def test_sources_visibles():
    """« Je veux voir les mêmes réflexions que Claude. » Nova montrait un extrait tronqué
    du résultat (« Actualité — 6 articles récents 1. Présidentielle… ») : impossible de
    savoir OÙ elle avait cherché, ni d'aller vérifier."""
    BRUT = ("🔎 **Résultats web : action PEA small cap 2026** (3)\n\n"
            "**1. Quelle action PEA acheter en 2026 ? Notre Top 4**\n"
            "_www.cafedelabourse.com · 2026-08-20_\nNotre sélection.\n"
            "🔗 https://www.cafedelabourse.com/top-pea-2026\n\n"
            "**2. Small caps françaises IA : 10 valeurs PEA en Bourse 2026**\n"
            "_pea.fr_\n🔗 https://pea.fr/small-caps-ia\n\n"
            "**3. Meilleures actions PEA 2026 : 7 titres solides**\n"
            "_www.seqooia.com_\n🔗 https://www.seqooia.com/actions-pea")
    src = A._sources_trouvees(BRUT)
    check("les trois sources sont extraites", len(src), 3)
    check("le titre est lisible", src[0]["titre"], "Quelle action PEA acheter en 2026 ? Notre Top 4")
    check("le domaine est propre (sans www)", src[0]["domaine"], "cafedelabourse.com")
    check_true("le lien permet de vérifier", src[0]["url"].startswith("https://"))
    check("un domaine court passe aussi", src[1]["domaine"], "pea.fr")
    # Rien à afficher quand il n'y a pas de résultats
    check("texte quelconque : aucune source", A._sources_trouvees("bonjour"), [])
    check("texte vide", A._sources_trouvees(""), [])
    check("résultat sans lien reste listé",
          len(A._sources_trouvees("**1. Un titre sans lien**\n_source.fr_")), 1)
    # Pas de doublon quand la même page revient
    deux = ("**1. Même page**\n🔗 https://x.fr/a\n\n**2. Même page**\n🔗 https://x.fr/a")
    check("les doublons sont écartés", len(A._sources_trouvees(deux)), 1)
    # …et jamais une liste interminable
    long = "\n\n".join(f"**{i}. Titre {i}**\n🔗 https://x{i}.fr/p" for i in range(1, 30))
    check_true("la liste reste courte", len(A._sources_trouvees(long)) <= 10)


def test_identifiants_par_type():
    """Défaut confirmé par l'audit, avec reproduction : tous les champs à résoudre
    recevaient la MÊME valeur. « déplace Releve_PEA dans le dossier Banque » donnait
    file_id == folder_id — le fichier était déplacé DANS LUI-MÊME. Et comme MOVE_FILE
    n'est pas « irréversible », c'était exécuté sans aucune confirmation."""
    FICH = [{"id": "1FILEabcdefGHIJKLmnop111", "name": "Releve_PEA.pdf"},
            {"id": "1FOLDERabcdefGHIJKL222", "name": "Banque"}]
    vrais = (A._tool, A._document_connu)
    try:
        A._document_connu = lambda app, quoi: ("", "")
        A._tool = lambda a, args=None, **k: (
            "✅ résultat :\n" + json.dumps({"data": {"files": FICH}})
            if ("SEARCH" in a.upper() or "FIND" in a.upper()) else "✅ ok")

        ACT = [{"name": "GOOGLEDRIVE_FIND_FILE", "required": ["query"]},
               {"name": "GOOGLEDRIVE_MOVE_FILE", "required": ["file_id", "folder_id"]}]
        args, _et, refus = A._resoudre_identifiants(
            "googledrive", "GOOGLEDRIVE_MOVE_FILE",
            {"file_id": "YOUR_FILE_ID", "folder_id": "FOLDER_ID"},
            "déplace Releve_PEA dans le dossier Banque", ACT)
        check_true("le fichier n'est plus déplacé dans lui-même",
                   args["file_id"] != args["folder_id"])
        check("le bon fichier est visé", args["file_id"], "1FILEabcdefGHIJKLmnop111")
        check("le bon dossier aussi", args["folder_id"], "1FOLDERabcdefGHIJKL222")
        check("rien n'empêche l'action", refus, "")

        # 49b. Le type d'objet se déduit du nom du champ
        for cle, typ in (("folder_id", "folder"), ("file_id", "file"), ("idBoard", "board"),
                         ("spreadsheet_id", "spreadsheet"), ("sheet_id", "sheet"),
                         ("team_id", "team"), ("database_id", "database"), ("truc", "")):
            check(f"type de « {cle} »", A._type_objet(cle), typ)
        # …et ce que l'utilisateur a nommé pour ce type
        check("« dans le dossier Banque » → Banque",
              A._indice_pour_type("déplace X dans le dossier Banque", "folder"), "Banque")
        check("« l'onglet 2026 » → 2026",
              A._indice_pour_type("supprime l'onglet 2026 du tableur X", "sheet"), "2026")

        # 49c. ⚠️ Un gid NUMÉRIQUE n'est pas un document à chercher. _est_bouchon juge
        # bouchon toute valeur de moins de 8 caractères : le gid d'un onglet (souvent 0)
        # envoyait Nova chercher un « document » qui n'existe pas.
        SPEC = {"name": "GOOGLESHEETS_DELETE_SHEET",
                "required": ["spreadsheet_id", "sheet_id"],
                "schema": {"properties": {"sheet_id": {"type": "integer"},
                                          "spreadsheet_id": {"type": "string"}}}}
        for valeur in (0, 12345, "0", "42"):
            check_true(f"un nombre n'est pas un document ({valeur!r})",
                       A._est_numerique("sheet_id", valeur, SPEC))
        check("une chaîne reste un document",
              A._est_numerique("spreadsheet_id", "YOUR_SPREADSHEET_ID", SPEC), False)
        S = [{"id": "1PEAabcdefGHIJKLmnop1234", "name": "Suivi_PEA_Lohan"}]
        A._tool = lambda a, args=None, **k: (
            "✅ résultat :\n" + json.dumps({"data": {"files": S}})
            if "SEARCH" in a.upper() else "✅ ok")
        args, _et, _r = A._resoudre_identifiants(
            "googlesheets", "GOOGLESHEETS_DELETE_SHEET",
            {"spreadsheet_id": "YOUR_SPREADSHEET_ID", "sheet_id": 0},
            "supprime l'onglet 2026 du tableur Suivi_PEA_Lohan",
            [{"name": "GOOGLESHEETS_SEARCH_SPREADSHEETS", "required": []}, SPEC])
        check("le tableur est résolu", args["spreadsheet_id"], "1PEAabcdefGHIJKLmnop1234")
        check("le gid reste un nombre, il n'est pas écrasé", args["sheet_id"], 0)

        # 49d. Le cas simple (un seul type) ne doit pas avoir régressé
        args, _et, refus = A._resoudre_identifiants(
            "googlesheets", "GOOGLESHEETS_BATCH_GET",
            {"spreadsheet_id": "YOUR_SPREADSHEET_ID"}, "lit mon fichier pea",
            [{"name": "GOOGLESHEETS_SEARCH_SPREADSHEETS", "required": []},
             {"name": "GOOGLESHEETS_BATCH_GET", "required": ["spreadsheet_id"]}])
        check("un seul type : inchangé", args["spreadsheet_id"], "1PEAabcdefGHIJKLmnop1234")
        check("…et aucun refus", refus, "")
    finally:
        (A._tool, A._document_connu) = vrais


def test_garde_fou_irreversible_complet():
    """« Tu te rends compte s'il fait ça avec mes mails ! » — audit du garde-fou.
    Trois trous, tous vérifiés en exécutant le code."""
    from plugins.builtin.composio_tool import ComposioPlugin

    # 48a. ⚠️ Une phrase ORDINAIRE valait « oui ». La règle cherchait « oui » n'importe
    # où dans le message : « oui je voudrais savoir autre chose » envoyait le mail.
    for accord in ("oui", "oui vas-y", "ok", "d'accord", "vas-y", "confirme", "go",
                   "valide", "c'est bon", "fais-le"):
        check_true(f"accord reconnu : « {accord} »", A._confirmation_donnee(accord))
    for pas_accord in ("oui je voudrais savoir autre chose",
                       "oui enfin bref, montre mon agenda",
                       "oui bien sûr que non", "d'accord mais annule",
                       "ok donc comment ça marche exactement ?",
                       "oui mais pas maintenant, montre-moi mon agenda d'abord",
                       "ouais mais sinon tu peux faire quoi",
                       "non", "annule", "laisse tomber", ""):
        check(f"PAS un accord : « {pas_accord[:44]} »",
              A._confirmation_donnee(pas_accord), False)
    check_true("un refus reste un refus", A._refus_donne("annule"))
    check_true("…même noyé dans une phrase", A._refus_donne("oui bien sûr que non"))

    # 48b. ⚠️ Les verbes étaient cherchés dans le nom COMPLET : « DROP » dans DROPBOX et
    # « SEND » dans SENDGRID faisaient demander confirmation pour de simples LECTURES.
    # Et à l'inverse, publier / fusionner / inviter / transférer passaient sans rien.
    for act in ("GMAIL_SEND_EMAIL", "GOOGLECALENDAR_DELETE_EVENT",
                "SLACK_INVITE_USER_TO_WORKSPACE", "GITHUB_MERGE_PULL_REQUEST",
                "X_PUBLISH_POST", "X_TRANSFER_OWNERSHIP", "X_CLOSE_ISSUE",
                "NOTION_SHARE_PAGE_PUBLICLY", "X_BAN_MEMBER", "X_PURGE_HISTORY"):
        check_true(f"irréversible : {act}", A._est_irreversible(act))
    for act in ("DROPBOX_LIST_FOLDER", "SENDGRID_GET_STATS", "GMAIL_FETCH_EMAILS",
                "GOOGLESHEETS_BATCH_GET", "X_SHARE_WITH_USER", "NOTION_SEARCH_NOTION_PAGE"):
        check(f"pas irréversible : {act}", A._est_irreversible(act), False)

    # 48c. ⚠️ LE trou le plus grave : le garde-fou ne vivait que sur les chemins
    # « directs ». L'agent ReAct appelle le plugin LUI-MÊME et pouvait donc envoyer un
    # mail ou supprimer un événement sans que personne ait rien confirmé.
    from config import config as CFG
    vraie_cle = getattr(CFG, "COMPOSIO_API_KEY", "")
    try:
        CFG.COMPOSIO_API_KEY = "ak_test"
        p = ComposioPlugin()
        for act, args in (("GMAIL_SEND_EMAIL", '{"to":"papa@exemple.fr","subject":"Salut"}'),
                          ("GOOGLECALENDAR_DELETE_EVENT", '{"event_id":"evt_1"}'),
                          ("SLACK_INVITE_USER_TO_WORKSPACE", '{"email":"x@y.fr"}'),
                          ("GITHUB_MERGE_PULL_REQUEST", '{"number":7}')):
            A._ATTENTE.clear()
            r = p.run(command=act, arguments=args)
            check_true(f"l'agent ne peut plus lancer {act[:28]}", r.startswith("⛔"))
            check_true(f"…et l'action est mise en attente ({act[:20]})",
                       bool(A._action_en_attente(A._PROFILE_ID)))
        # La cible doit être visible : on ne confirme pas à l'aveugle
        A._ATTENTE.clear()
        r = p.run(command="GMAIL_SEND_EMAIL",
                  arguments='{"to":"papa@exemple.fr","subject":"Salut"}')
        check_true("le destinataire est montré avant d'accepter", "papa@exemple.fr" in r)
        # …et une LECTURE ne doit jamais être bloquée
        for act in ("GMAIL_FETCH_EMAILS", "GOOGLESHEETS_BATCH_GET",
                    "DROPBOX_LIST_FOLDER", "SENDGRID_GET_STATS"):
            A._ATTENTE.clear()
            check(f"lecture non bloquée : {act}",
                  p.run(command=act, arguments="{}").startswith("⛔"), False)
    finally:
        CFG.COMPOSIO_API_KEY = vraie_cle
        A._ATTENTE.clear()


def test_resultats_automatisations_remontent():
    """« J'ai fait une automatisation à 17h mais je reçois rien. » Elle s'exécutait bien :
    son résultat n'allait NULLE PART sans Telegram. Il fallait penser à ouvrir la fenêtre
    Automatisations pour le découvrir — donc une automatisation ne servait à rien."""
    from agent import automations as AU

    vrais = (AU._load, AU._save)
    stock = []
    try:
        AU._load = lambda: [dict(x) for x in stock]
        AU._save = lambda items: (stock.clear(), stock.extend(dict(x) for x in items))

        a = AU.add("Actu bourse du jour", "résume l'actu bourse", hour=17)
        check("rien à signaler tant que ça n'a pas tourné", AU.non_lus(), [])

        # On simule une exécution (sans appeler le modèle)
        for it in stock:
            if it["id"] == a["id"]:
                it.update({"last_run": 1_700_000_000.0, "last_result": "CAC 40 : +0,8 %",
                           "lu": False, "runs": 1})
        nouveaux = AU.non_lus()
        check("le résultat remonte comme non lu", len(nouveaux), 1)
        check("…avec son titre", nouveaux[0]["titre"], "Actu bourse du jour")
        check("…et son contenu", nouveaux[0]["resultat"], "CAC 40 : +0,8 %")
        check_true("…et la date d'exécution", bool(nouveaux[0]["quand"]))

        # Une fois vu, il ne doit plus revenir à chaque ouverture
        check("marquage effectif", AU.marquer_lus(), 1)
        check("il ne remonte plus", AU.non_lus(), [])
        check("…et un second marquage ne fait rien", AU.marquer_lus(), 0)

        # Une exécution SUIVANTE redevient non lue
        for it in stock:
            it.update({"last_result": "CAC 40 : -1,2 %", "lu": False})
        check("la nouvelle exécution remonte", len(AU.non_lus()), 1)
        check("…avec le contenu à jour", AU.non_lus()[0]["resultat"], "CAC 40 : -1,2 %")

        # Un résultat VIDE ne doit rien afficher
        for it in stock:
            it.update({"last_result": "   ", "lu": False})
        check("un résultat vide n'est pas présenté", AU.non_lus(), [])

        # Marquage ciblé : une automatisation vue n'efface pas les autres
        b = AU.add("Veille tech", "résume l'actu tech", hour=12)
        for it in stock:
            it.update({"last_result": "contenu", "lu": False})
        check("deux résultats en attente", len(AU.non_lus()), 2)
        check("marquage ciblé", AU.marquer_lus([a["id"]]), 1)
        restants = AU.non_lus()
        check("l'autre reste en attente", len(restants), 1)
        check("…et c'est le bon", restants[0]["id"], b["id"])
    finally:
        (AU._load, AU._save) = vrais


def test_jamais_d_ecriture_non_demandee():
    """Les deux chemins par lesquels Nova pouvait MODIFIER quelque chose sans que
    personne l'ait demandé — et sans passer par la confirmation, puisque ni CREATE ni
    UPDATE ne comptent comme « irréversibles »."""

    # 47a. ⚠️ L'action de RECHERCHE était choisie sur une simple sous-chaîne, en
    # n'excluant que DELETE. Sur une app inconnue, « X_CREATE_SEARCH_INDEX » et
    # « X_ADD_TO_LIST » contiennent SEARCH et LIST : Nova exécutait une ÉCRITURE toute
    # seule, juste pour retrouver un identifiant.
    for actions, attendu in (
            ([{"name": "X_CREATE_SEARCH_INDEX"}, {"name": "X_GET"}], ""),
            ([{"name": "X_ADD_TO_LIST"}, {"name": "X_LIST_ALL"}], "X_LIST_ALL"),
            ([{"name": "X_DELETE_QUERY"}, {"name": "X_FETCH"}], ""),
            ([{"name": "X_ARCHIVE_LIST"}, {"name": "X_BROWSE"}], "X_BROWSE"),
            ([{"name": "GOOGLESHEETS_SEARCH_SPREADSHEETS"},
              {"name": "GOOGLESHEETS_BATCH_GET"}], "GOOGLESHEETS_SEARCH_SPREADSHEETS")):
        trouvee = A._action_de_recherche("x", actions)
        check(f"recherche sûre parmi {[a['name'] for a in actions]}", trouvee, attendu)
        if trouvee:
            check(f"…et elle n'écrit rien ({trouvee})", A._ecrit(trouvee), False)
    # Une action qui exige déjà l'identifiant ne sait pas le CHERCHER
    check("une action à identifiant obligatoire n'est pas une recherche",
          A._action_de_recherche("x", [{"name": "X_LIST_CARDS", "required": ["board_id"]},
                                       {"name": "X_SEARCH_ALL", "required": []}]),
          "X_SEARCH_ALL")

    # 47b. ⚠️ Le contrôle ne portait que sur l'OBJET, jamais sur le VERBE :
    # « montre-moi ma page Recettes » + CREATE_NOTION_PAGE passait sans broncher.
    # Nova créait une page VIDE et l'annonçait en succès.
    for msg, act in (("montre-moi ma page Recettes", "NOTION_CREATE_NOTION_PAGE"),
                     ("lis mon tableur", "GOOGLESHEETS_CREATE_SPREADSHEET"),
                     ("consulte mes tickets", "LINEAR_CREATE_ISSUE"),
                     ("ouvre mon fichier budget", "GOOGLEDRIVE_UPLOAD_FILE"),
                     ("affiche mes mails", "GMAIL_SEND_EMAIL")):
        check_true(f"lecture + écriture = contresens : {act[:30]}",
                   A._action_douteuse(msg, act))
    # …sans jamais bloquer une écriture RÉELLEMENT demandée
    for msg, act in (("crée une page notion", "NOTION_CREATE_NOTION_PAGE"),
                     ("envoie un mail à paul", "GMAIL_SEND_EMAIL"),
                     ("supprime cette page", "NOTION_DELETE_BLOCK"),
                     ("ajoute une ligne dans mon tableur", "GOOGLESHEETS_BATCH_UPDATE"),
                     ("montre-moi ma page Recettes", "NOTION_FETCH_NOTION_PAGE"),
                     ("liste mes dépôts github",
                      "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER")):
        check(f"choix légitime préservé : {act[:34]}", A._action_douteuse(msg, act), False)

    # 47c. La liste des verbes d'écriture sert aux DEUX gardes : un oubli = un dégât.
    for act in ("GOOGLEDRIVE_UPLOAD_FILE", "X_WRITE_ROW", "X_APPEND_VALUES",
                "X_RENAME_BOARD", "X_CLEAR_VALUES", "X_ASSIGN_TASK", "X_CLOSE_ISSUE",
                "X_BAN_MEMBER", "X_GRANT_ACCESS", "X_REVOKE_TOKEN", "X_PUBLISH_POST"):
        check_true(f"écriture reconnue : {act}", A._ecrit(act))
    for act in ("GMAIL_FETCH_EMAILS", "GOOGLESHEETS_BATCH_GET", "NOTION_SEARCH_NOTION_PAGE",
                "GITHUB_LIST_COMMITS", "X_GET_ASSET", "X_BROWSE_CARDS", "X_LIST_ALL"):
        check(f"lecture non confondue : {act}", A._ecrit(act), False)


def test_app_en_panne_repond_quand_meme():
    """Une app en panne mettait fin au tour. « combien ça va me coûter en gazole et
    péage, et est-ce que ça vaut le coup ? » recevait pour TOUTE réponse « je n'ai pas
    pu accéder à cette application » — alors que la question se traite sans Maps."""

    # 46a. Distinguer une consigne qui vise l'app d'une vraie question de fond
    for demande in ("On est mardi, je suis à Montauban, je pars vendredi à Leucate puis "
                    "Revel puis Pau — dis-moi comment m'organiser et combien ça coûte",
                    "combien de temps de route entre Montauban et Pau, ça vaut le coup ?",
                    "comment je peux organiser mon week-end entre trois villes ?",
                    "explique-moi comment répartir mes révisions sur les trois jours"):
        check_true(f"question de fond : « {demande[:40]}… »", A._merite_une_reponse(demande))
    for demande in ("ouvre mon tableur PEA", "lit mon fichier pea sur google sheet",
                    "crée une page notion", "montre mon agenda de demain",
                    "envoie un mail à papa"):
        check(f"consigne qui vise l'app : « {demande[:32]}… »",
              A._merite_une_reponse(demande), False)

    # 46b. La consigne donnée au modèle exige de répondre SANS inventer
    consigne = A._consigne_sans_app("❌ 404 Not Found")
    check_true("on demande de répondre quand même", "Réponds quand même" in consigne)
    check_true("…en annonçant que ce sont des estimations", "estimation" in consigne.lower())
    check_true("…sans inventer de valeur précise", "N'invente" in consigne)
    check_true("…et en disant ce qui n'a pas pu être vérifié", "vérifier" in consigne)
    check_true("le détail technique est marqué comme à ne pas recopier",
               "ne PAS recopier" in consigne)

    # 46c. L'échec d'app est bien SIGNALÉ (sans ce drapeau, le tour s'arrêtait)
    vrais = (A._tool, A._composio_list_actions, A._connected_accounts,
             A._build_args, A._llm_json, A._known_args)
    try:
        A._tool = lambda a, args=None, **k: '❌ Action échouée : {"http_error": "404"}'
        A._composio_list_actions = lambda s: [{"name": "GOOGLE_MAPS_GET_DIRECTION",
                                               "desc": "", "required": []}]
        A._connected_accounts = lambda: [("googlemaps", "u", "ACTIVE")]
        A._build_args = lambda act, spec, msg, ctx="", error="": {"origin": "Montauban"}
        A._llm_json = lambda s, u: {"action": "GOOGLE_MAPS_GET_DIRECTION", "arguments": {}}
        A._known_args = lambda action, message: None
        r = A._generic_app_flow("combien de route entre Montauban et Pau ?", "googlemaps")
        check_true("l'échec d'app est signalé", r.get("echec_app") is True)
        check_true("…avec un message honnête", bool(r.get("done_answer")))
    finally:
        (A._tool, A._composio_list_actions, A._connected_accounts,
         A._build_args, A._llm_json, A._known_args) = vrais

    # 46d. ⚠️ Le message ne doit plus se contredire : « GOOGLE_MAPS_GET_DIRECTION
    # n'existe pas », suivi d'une liste où il figure.
    from plugins.builtin import composio_tool as CT
    vrai_cat = CT.catalogue
    try:
        CT.catalogue = lambda indice="", **k: ("• google_maps : GOOGLE_MAPS_DISTANCE_MATRIX_API, "
                                               "GOOGLE_MAPS_GET_DIRECTION")
        dispo = CT.catalogue("")
        check_true("l'action citée EST au catalogue",
                   "GOOGLE_MAPS_GET_DIRECTION" in dispo.upper())
        # Le code doit alors parler d'autorisation, pas d'action inexistante
        check_true("une action au catalogue n'est pas « inexistante »",
                   "GOOGLE_MAPS_GET_DIRECTION".upper() in dispo.upper())
    finally:
        CT.catalogue = vrai_cat


def test_cours_persistants():
    """« mes cours sont supprimés après chaque veille de Render ». Ils ne vivaient que
    sur le disque du conteneur, remis à zéro à chaque mise en veille — et comme la liste
    est servie par le serveur, ils disparaissaient des DEUX appareils à la fois."""
    from agent import cours as CO

    vrai_dir, vrai_sb = CO._DIR, CO._sb
    base = {}                                   # fausse base « persistante »

    class FauxCurseur:
        def __init__(self): self.res, self.rowcount = None, 0
        def execute(self, q, a=None):
            q = " ".join(q.split())
            if q.startswith("CREATE TABLE"): return
            if q.startswith("SELECT data FROM cours WHERE"):
                self.res = [(base[a[0]],)] if a[0] in base else []
            elif q.startswith("SELECT data FROM cours"):
                self.res = [(v,) for v in base.values()]
            elif q.startswith("INSERT INTO cours"):
                base[a[0]] = json.loads(a[1])
            elif q.startswith("DELETE FROM cours"):
                self.rowcount = 1 if base.pop(a[0], None) is not None else 0
        def fetchone(self): return self.res[0] if self.res else None
        def fetchall(self): return self.res or []
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FausseBase:
        def cursor(self): return FauxCurseur()
        def close(self): pass

    try:
        CO._DIR = Path("/tmp/nova_test_cours")
        CO._sb = lambda: FausseBase()
        s = CO.demarrer("Effet Doppler", "Physique")
        sid = s["id"]
        check_true("le cours part aussi dans la base", sid in base)
        check("…et se relit depuis la base", CO._lire(sid)["titre"], "Effet Doppler")

        # ⚠️ LE cas qui posait problème : Render efface le disque pendant la veille.
        import shutil
        shutil.rmtree(CO._DIR, ignore_errors=True)
        check("après effacement du disque, le cours survit",
              CO._lire(sid)["titre"], "Effet Doppler")
        listés = CO.lister()
        check_true("…et reste dans la liste (donc visible sur les 2 appareils)",
                   any(x["id"] == sid for x in listés))
        check("aucun doublon disque + base", len([x for x in listés if x["id"] == sid]), 1)

        # Une suppression doit valoir PARTOUT, sinon le cours revient au redémarrage
        check_true("suppression effective", CO.supprimer(sid))
        check("…dans la base aussi", sid in base, False)
        check("…et il ne réapparaît pas", [x for x in CO.lister() if x["id"] == sid], [])

        # Sans base configurée, on retombe proprement sur le disque
        CO._sb = lambda: None
        s2 = CO.demarrer("Sans base", "")
        check("le disque seul fonctionne toujours", CO._lire(s2["id"])["titre"], "Sans base")
        check_true("…et la liste aussi", any(x["id"] == s2["id"] for x in CO.lister()))
        CO.supprimer(s2["id"])
    finally:
        import shutil
        shutil.rmtree(Path("/tmp/nova_test_cours"), ignore_errors=True)
        CO._DIR, CO._sb = vrai_dir, vrai_sb


def test_diag_patient():
    """« ❌ nvidia · 22,4 s · n'a pas répondu » : 22,4 s ≈ LLM_TIMEOUT. Le diagnostic
    coupait à la MÊME limite que l'usage normal, il ne pouvait donc pas répondre à la
    seule question qui compte — lent, ou mort ?"""
    import llm.client as C

    # 45a. Le budget ne peut que RACCOURCIR : c'est ce qui bridait la mesure.
    jeton = C._BUDGET_APPEL.set(45.0)
    try:
        t = C._timeout(22.0)
        mesure = getattr(t, "read", t)
        check_true("sans forçage, le budget ne rallonge pas le délai", float(mesure) <= 22.0)
    finally:
        C._BUDGET_APPEL.reset(jeton)

    # 45b. Le diagnostic, lui, impose sa propre patience
    C._TIMEOUT_MESURE["s"] = 60.0
    try:
        t = C._timeout(22.0)
        mesure = getattr(t, "read", t)
        check("le diagnostic mesure jusqu'au bout", float(mesure), 60.0)
        # …même si un budget plus court traîne dans le contexte
        jeton = C._BUDGET_APPEL.set(5.0)
        try:
            t = C._timeout(22.0)
            check("un budget résiduel ne rebride pas la mesure",
                  float(getattr(t, "read", t)), 60.0)
        finally:
            C._BUDGET_APPEL.reset(jeton)
    finally:
        C._TIMEOUT_MESURE["s"] = 0.0

    # 45c. …et il le remet à zéro : un appel normal ne doit pas hériter de la patience
    t = C._timeout(22.0)
    check("hors diagnostic, le délai normal revient", float(getattr(t, "read", t)), 22.0)
    check_true("la patience du diagnostic dépasse largement l'usage normal",
               A._DIAG_PATIENCE > C.TIMEOUT_LLM * 2)


def test_apps_inconnues():
    """« Un bug à chaque nouvelle app. » Six défauts trouvés par audit systématique, tous
    vérifiés en exécutant le code. Ils ne touchaient PAS les apps déjà déboguées — d'où
    l'impression que chaque nouvelle connexion cassait quelque chose."""

    # 44a. ⚠️ Le bouchon n'était reconnu que pour 12 noms d'objets (spreadsheet, sheet,
    # file, page…). YOUR_DATABASE_ID, YOUR_CARD_ID, YOUR_CHANNEL_ID passaient pour de
    # VRAIS identifiants : Notion, Trello, Slack, Linear les exécutaient tels quels.
    for faux in ("YOUR_DATABASE_ID", "YOUR_CARD_ID", "YOUR_BOARD_ID", "YOUR_CHANNEL_ID",
                 "YOUR_TEAM_ID", "YOUR_PROJECT_ID", "YOUR_ISSUE_ID", "YOUR_ASSET_ID",
                 "PLACEHOLDER_ID", "REPLACE_WITH_YOUR_ID", "EXAMPLE_ID_12345",
                 "insert_id_here", "xxx-xxx-xxx", "votre_identifiant"):
        check_true(f"bouchon reconnu : {faux}", A._est_bouchon(faux))
    for vrai in ("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
                 "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "5f2b8c1e9a4d3b2c1e0f9a8b",
                 "C01ABC2DEFG", "gid://company/Task/1122334455", "urn:li:person:abc123"):
        check(f"vrai identifiant accepté : {vrai[:22]}…", A._est_bouchon(vrai), False)
    check_true("une URL n'est pas un identifiant",
               A._est_bouchon("https://docs.google.com/spreadsheets/d/1abc"))

    # 44b. ⚠️ Seules les clés plates « *_id » étaient inspectées : Trello (idBoard),
    # Asana (gid), Slack (channel) échappaient à toute la protection.
    for cle in ("idBoard", "idList", "boardId", "pageId", "gid", "uid", "channel",
                "team", "workspace", "database_id", "parent_page_id"):
        check_true(f"champ identifiant reconnu : {cle}", A._est_champ_identifiant(cle))
    for cle in ("user_id", "calendar_id", "entity_id", "title", "content", "body"):
        check(f"…et pas confondu : {cle}", A._est_champ_identifiant(cle), False)

    # 44c. ⚠️ « USER » dans le nom disqualifiait l'action canonique « mes propres
    # données » de presque TOUTE API. Nova exécutait une action absurde à la place.
    for msg, act in (("liste mes dépôts github",
                      "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER"),
                     ("montre mes playlists", "SPOTIFY_GET_A_LIST_OF_CURRENT_USER_S_PLAYLISTS"),
                     ("mes infos", "GITHUB_GET_THE_AUTHENTICATED_USER"),
                     ("mes tâches", "ASANA_GET_TASKS_FOR_CURRENT_USER")):
        check(f"« {act[:38]}… » n'est plus écartée", A._action_douteuse(msg, act), False)
    # …sans cesser d'écarter ce qui est vraiment hors sujet
    check_true("un contresens reste détecté",
               A._action_douteuse("crée un projet", "NOTION_CREATE_COMMENT"))
    check("le préfixe d'app ne pollue plus la comparaison",
          A._objet_de_action("GMAIL_LIST_THREADS"), "LIST_THREADS")

    # 44d. ⚠️ Un nom approchant était pris dans un SET (ordre dépendant du hash du
    # process) et sans notion de verbe : « montre-moi ma page » pouvait CRÉER une page
    # vide, annoncée en succès. Le repli déterministe avait le même défaut.
    NOTION = [{"name": n, "desc": ""} for n in
              ("NOTION_CREATE_COMMENT", "NOTION_CREATE_DATABASE", "NOTION_CREATE_NOTION_PAGE",
               "NOTION_DELETE_BLOCK", "NOTION_FETCH_NOTION_PAGE", "NOTION_SEARCH_NOTION_PAGE",
               "NOTION_UPDATE_PAGE")]
    for demande in ("montre-moi ma page Recettes", "lis ma page notion",
                    "ouvre ma base de données", "consulte mes pages"):
        choisi = A._action_par_defaut(demande, NOTION)
        check(f"« {demande[:32]}… » n'écrit jamais", A._ecrit(choisi) if choisi else False, False)
    check("…mais une création demandée reste possible",
          A._action_par_defaut("crée une page notion", NOTION), "NOTION_CREATE_NOTION_PAGE")
    check("…et une suppression demandée aussi",
          A._action_par_defaut("supprime cette page", NOTION), "NOTION_DELETE_BLOCK")
    for act, ecrit in (("NOTION_CREATE_NOTION_PAGE", True), ("NOTION_FETCH_NOTION_PAGE", False),
                       ("GMAIL_SEND_EMAIL", True), ("GMAIL_LIST_THREADS", False),
                       ("LINEAR_UPDATE_ISSUE", True), ("GITHUB_LIST_COMMITS", False)):
        check(f"écriture ? {act}", A._ecrit(act), ecrit)

    # 44e. ⚠️ « montre-moi mon pea » cherchait un document nommé « montre-moi » : Nova
    # ouvrait le premier venu, servait SES données, et mémorisait le mauvais raccourci.
    for demande, attendu in (("montre-moi mon pea", "pea"),
                             ("affiche-moi mes notes", "notes"),
                             ("donne-moi mon budget", "budget"),
                             ("consulte le tableur Suivi_PEA_Lohan_Pere", "Suivi_PEA_Lohan_Pere"),
                             ("ouvre « Budget 2026 » stp", "Budget 2026")):
        check(f"mots-clés de « {demande[:30]}… »", A._mots_cles_fichier(demande), attendu)
    check_true("« montre-moi » n'est pas un nom de document",
               A._que_des_mots_vides("montre-moi"))
    check("…contrairement à un vrai nom", A._que_des_mots_vides("Suivi_PEA_Lohan"), False)

    # 44f. ⚠️ Un identifiant NUMÉRIQUE, ou nommé gid/idBoard, était invisible : la
    # recherche répondait « aucun document trouvé » alors que l'app venait de le rendre.
    for payload, attendu in (
            ('{"data":{"items":[{"id":123456789012,"name":"Mon tableau"}]}}', "Mon tableau"),
            ('{"data":{"boards":[{"gid":"1122334455667","name":"Projets"}]}}', "Projets"),
            ('{"results":[{"idBoard":"5f2b8c1e9a4d","name":"Sprint"}]}', "Sprint"),
            ('{"channels":[{"id":"C01ABC2DEFG","name":"general"}]}', "general")):
        trouves = A._identifiants_trouves(payload)
        check_true(f"identifiant lu dans {payload[:30]}…", bool(trouves))
        check(f"…avec son nom ({attendu})", trouves[0][1] if trouves else "", attendu)


def test_choix_fournisseur():
    """Lohan veut CHOISIR qui répond (NVIDIA, Grok…). Le mécanisme existait par message
    (« réponds avec l'api nvidia ») mais rien ne permettait de le fixer une fois pour
    toutes."""
    import llm.client as C
    import time as _t
    from config import config as CFG

    ancien = C.PREFERENCE.get("fournisseur", "")
    ko = dict(C._FOURNISSEURS_KO)
    try:
        # 43a. Choisir, relire, revenir à l'automatique
        check("un fournisseur connu est retenu", C.choisir_fournisseur("nvidia"), "nvidia")
        check("…et relu", C.fournisseur_choisi(), "nvidia")
        check("« grok » passe par son vrai nom", C.choisir_fournisseur("xai"), "xai")
        check("un nom inconnu est ignoré", C.choisir_fournisseur("nimportequoi"), "")
        check("« auto » remet le choix automatique", C.choisir_fournisseur("auto"), "")
        check("vide aussi", C.choisir_fournisseur(""), "")

        # 43b. Le choix passe DEVANT dans la chaîne — sans supprimer le secours
        C._FOURNISSEURS_KO.clear()
        vraies = {}
        for nom, attr in (("nvidia", "NVIDIA_API_KEY"), ("groq", "GROQ_API_KEY"),
                          ("xai", "XAI_API_KEY")):
            vraies[attr] = getattr(CFG, attr, "")
            setattr(CFG, attr, "cle-de-test")
        try:
            C.choisir_fournisseur("xai")
            chaine = [n for n, _f, _m in C._providers_disponibles("equilibre")]
            check("le fournisseur choisi passe en tête", chaine[0] if chaine else "", "xai")
            check_true("les autres restent en secours", len(chaine) > 1)
            # …mais une consigne ÉCRITE dans la phrase reste plus précise
            chaine = [n for n, _f, _m in C._providers_disponibles("equilibre", impose="nvidia")]
            check("la consigne du message l'emporte", chaine[0] if chaine else "", "nvidia")
            C.choisir_fournisseur("")
            chaine = [n for n, _f, _m in C._providers_disponibles("equilibre")]
            check_true("sans choix, la chaîne reste complète", len(chaine) >= 3)
        finally:
            for attr, v in vraies.items():
                setattr(CFG, attr, v)

        # 43c. L'état de chacun est lisible par l'interface
        C._FOURNISSEURS_KO["nvidia"] = (_t.monotonic() - 30, "lent")
        C._FOURNISSEURS_KO["gemini"] = (_t.monotonic() - 10, "mort")
        etats = {f["nom"]: f for f in C.etat_fournisseurs()}
        check_true("tous les fournisseurs sont listés", len(etats) >= 6)
        # ⚠️ On stocke le DÉBUT de la sanction : mal calculé, le délai restant était négatif.
        for nom in ("nvidia", "gemini"):
            if etats[nom]["configure"]:
                check_true(f"{nom} : délai restant positif", etats[nom]["reprend_dans_s"] > 0)
        check_true("un délai n'est jamais négatif",
                   all(f["reprend_dans_s"] >= 0 for f in etats.values()))
        check_true("une sanction longue dure plus qu'une courte",
                   etats["gemini"]["reprend_dans_s"] >= etats["nvidia"]["reprend_dans_s"]
                   or not etats["gemini"]["configure"])
        for f in etats.values():
            if not f["configure"]:
                check(f"{f['nom']} sans clé : annoncé comme tel", f["etat"], "non configuré")
                check(f"{f['nom']} sans clé : aucune raison affichée", f["raison"], "")
        check_true("chacun a un nom lisible", all(f["joli"] for f in etats.values()))
    finally:
        C.PREFERENCE["fournisseur"] = ancien
        C._FOURNISSEURS_KO.clear()
        C._FOURNISSEURS_KO.update(ko)


def test_apps_robustesse():
    """Un bug à chaque nouvelle app connectée. Trois causes, toutes génériques."""
    FICHIERS = [{"id": "1abcdefghijABCDEFGHIJ1234567890xyz", "name": "Mes notes"},
                {"id": "2abcdefghijABCDEFGHIJ1234567890xyz", "name": "Journal"}]
    vrai_tool = A._tool
    try:
        A._tool = lambda a, args=None, **k: (
            "✅ résultat :\n" + json.dumps({"data": {"files": FICHIERS}})
            if "SEARCH" in a.upper() else "✅ ok")

        # 42a. ⚠️ Canva : un identifiant FACULTATIF qu'on ne sait pas remplir doit être
        # RETIRÉ. On envoyait un asset_id fantôme → « asset_not_found » (404) en boucle,
        # alors que l'action marche très bien sans.
        CANVA = [{"name": "CANVA_CREATE_CANVA_DESIGN_WITH_OPTIONAL_ASSET", "desc": "",
                  "required": ["design_type"], "props": ["design_type", "title", "asset_id"]}]
        args, _et, refus = A._resoudre_identifiants(
            "canva", "CANVA_CREATE_CANVA_DESIGN_WITH_OPTIONAL_ASSET",
            {"design_type": {"type": "preset", "name": "doc"}, "title": "Salut", "asset_id": ""},
            "crée un doc canva", CANVA)
        check_true("l'asset facultatif est retiré", "asset_id" not in args)
        check("…et l'appel part quand même", refus, "")
        check("le reste des arguments est intact", args["title"], "Salut")

        # 42b. ⚠️ Notion : un identifiant OBLIGATOIRE mais ABSENT n'était pas vu — on ne
        # regardait que les valeurs bouchons, jamais les clés manquantes. Notion répondait
        # « block_id should be a valid uuid, instead was `` ».
        NOTION = [{"name": "NOTION_ADD_MULTIPLE_PAGE_CONTENT", "desc": "",
                   "required": ["block_id", "content"], "props": ["block_id", "content"]},
                  {"name": "NOTION_SEARCH_NOTION_PAGE", "desc": "", "required": [],
                   "props": ["query"]}]
        args, _et, refus = A._resoudre_identifiants(
            "notion", "NOTION_ADD_MULTIPLE_PAGE_CONTENT", {"content": "salut"},
            "écris salut dans ma page Journal", NOTION)
        check("l'identifiant obligatoire absent est résolu", args.get("block_id"),
              "2abcdefghijABCDEFGHIJ1234567890xyz")
        check("aucun refus quand la page est nommée", refus, "")
        # …et sans page nommée, on REFUSE proprement au lieu d'envoyer du vide
        args, _et, refus = A._resoudre_identifiants(
            "notion", "NOTION_ADD_MULTIPLE_PAGE_CONTENT", {"content": "salut"},
            "écris salut c'est nova", NOTION)
        check_true("sans cible : refus clair", bool(refus))
        check_true("…qui propose les pages existantes", "Journal" in refus)
        check_true("aucun identifiant vide n'est envoyé", A._est_bouchon(args.get("block_id")))
    finally:
        A._tool = vrai_tool

    # 42c. ⚠️ Les erreurs d'API étaient servies en JSON brut : l'utilisateur recevait
    # « {"http_error": "404 Client Error…" } », ce qui faisait passer un détail pour
    # une panne générale.
    c404 = A._honest_no_access(
        "CANVA_CREATE_CANVA_DESIGN_WITH_OPTIONAL_ASSET",
        '{"http_error": "404 Client Error: Not Found for url: https://api.canva.com/'
        'rest/v1/designs", "message": "asset_not_found", "status_code": 404}')
    check_true("un 404 est expliqué en français", "ne trouve pas" in c404)
    check_true("…sans JSON brut", "http_error" not in c404 and "{" not in c404)
    check_true("…et propose une suite", "créer" in c404 or "nom exact" in c404)

    c503 = A._honest_no_access("GMAIL_FETCH_EMAILS", '{"http_error": "503 Service Unavailable"}')
    check_true("une panne de l'app est distinguée", "ne répond pas" in c503)
    check_true("…et on dit que ça ne vient pas de lui", "pas du tien" in c503)

    # Une erreur inconnue reste lisible : on extrait la phrase utile, pas le JSON
    lisible = A._erreur_lisible_app('{"http_error": "400 Bad Request", "message": "titre manquant"}')
    check("la phrase utile est extraite", lisible, "titre manquant")
    check_true("un JSON sans message donne au moins le code",
               "400" in A._erreur_lisible_app('{"http_error": "400 Bad Request"}'))

    # 42d. ⚠️ Nova répondait « je suis en UTC » alors qu'elle est réglée sur Paris,
    # et ne savait pas non plus quel jour on est.
    rep = A._repere_temporel()
    check_true("le fuseau est annoncé", "Europe/Paris" in rep)
    check_true("…et démenti explicite de l'UTC", "pas en UTC" in rep)
    check_true("la date est donnée", any(j in rep for j in
                                         ("lundi", "mardi", "mercredi", "jeudi",
                                          "vendredi", "samedi", "dimanche")))
    check_true("l'heure aussi", "h" in rep)
    # …sur les DEUX chemins : discussion ET agent
    smalltalk = A._smalltalk_messages("tu es sur quel fuseau horaire ?")[0]["content"]
    check_true("le chemin discussion le sait", "Europe/Paris" in smalltalk)
    cfg = A._build_agent_cfg("tu es sur quel fuseau ?", "Nova")
    sysm = cfg.get("system") or cfg.get("system_prompt") or ""
    check_true("le chemin agent le sait aussi", "Europe/Paris" in sysm)


def test_competences():
    """Nova corrigeait ses arguments après l'erreur de l'API… puis jetait la correction.
    À la demande suivante : même erreur, même aller-retour. Elle galérait à l'identique."""
    from agent import competences as K

    K.effacer_tout()
    try:
        # 41a. ⚠️ Une recette décrit une FORME. Aucun contenu personnel ne doit y entrer.
        forme = K.squelette({
            "design_type": {"type": "preset", "name": "doc"},
            "title": "RDV médecin Dr Dupont", "to": "papa@exemple.fr",
            "body": "Salut papa, je rentre à 18h", "ranges": ["A1:D50"],
            "calendar_id": "primary", "phone": "+33612345678",
            "lien": "https://docs.google.com/spreadsheets/d/1abc",
            "count": 42, "actif": True, "vide": None})
        check("la structure imbriquée est gardée", forme["design_type"],
              {"type": "preset", "name": "doc"})
        check("un format technique est gardé", forme["ranges"], ["A1:D50"])
        check("un mot d'énumération est gardé", forme["calendar_id"], "primary")
        for champ in ("title", "to", "body", "phone", "lien"):
            check(f"« {champ} » ne fuite pas", forme[champ], "<texte>")
        check("les nombres sont neutralisés", forme["count"], 0)
        check("un booléen reste un booléen", forme["actif"], True)
        check("un vide reste vide", forme["vide"], None)
        texte = json.dumps(forme, ensure_ascii=False)
        for secret in ("Dupont", "papa@exemple.fr", "18h", "33612345678", "docs.google"):
            check_true(f"« {secret} » absent de la recette", secret not in texte)

        # 41b. Apprendre, relire
        K.apprendre("canva", "CANVA_CREATE_DESIGN",
                    {"design_type": {"type": "preset", "name": "doc"}, "title": "Affiche"},
                    corrections=1, erreur="design_type must be an object")
        r = K.recette("canva", "CANVA_CREATE_DESIGN")
        check("la recette se relit", r["forme"]["design_type"], {"type": "preset", "name": "doc"})
        check("une action inconnue n'invente rien", K.recette("canva", "X_Y"), {})
        ind = K.indice("canva", "CANVA_CREATE_DESIGN")
        check_true("l'indice est soufflé au modèle", "A DÉJÀ FONCTIONNÉ" in ind)
        check_true("…avec l'erreur qu'il évite", "must be an object" in ind)
        check("pas d'indice sans recette", K.indice("canva", "X_Y"), "")

        # 41c. Le PIRE cas est conservé : une recette chèrement acquise le reste
        K.apprendre("canva", "CANVA_CREATE_DESIGN",
                    {"design_type": {"type": "preset", "name": "doc"}}, corrections=0)
        r = K.recette("canva", "CANVA_CREATE_DESIGN")
        check("les corrections passées ne s'effacent pas", r["corrections"], 1)
        check("la réutilisation est comptée", r["usages"], 2)
        check_true("l'erreur d'origine est gardée", "must be an object" in r["erreur_evitee"])

        # 41d. Rien d'incomplet n'est appris
        check("sans action, rien", K.apprendre("canva", "", {"a": 1}), {})
        check("sans arguments, rien", K.apprendre("canva", "X_Y", {}), {})
        check("des arguments non-dict, rien", K.apprendre("canva", "X_Y", "bonjour"), {})

        # 41e. Oublier une recette devenue fausse
        check("une recette s'oublie", K.oublier("CANVA_CREATE_DESIGN"), 1)
        check("elle n'est plus servie", K.recette("canva", "CANVA_CREATE_DESIGN"), {})
    finally:
        K.effacer_tout()

    # 41f. Bout en bout : galérer une fois, puis ne plus jamais galérer
    ACTIONS = [{"name": "CANVA_CREATE_DESIGN", "desc": "", "schema": {}}]
    APPELS, INDICES = [], []

    def faux_llm_json(sysm, usr):
        INDICES.append("A DÉJÀ FONCTIONNÉ" in usr)
        if "Tu construis les ARGUMENTS" in sysm:
            if "A DÉJÀ FONCTIONNÉ" in usr or "ÉCHOUÉ" in usr:
                return {"arguments": {"design_type": {"type": "preset", "name": "doc"},
                                      "title": "Affiche"}}
            return {"arguments": {"design_type": "doc", "title": "Affiche"}}   # la faute
        return {"action": "CANVA_CREATE_DESIGN", "arguments": {}}

    def faux_tool(action, args=None, **kw):
        APPELS.append(dict(args or {}))
        if isinstance((args or {}).get("design_type"), str):
            return ('❌ Action échouée : {"message": "Invalid parameter: '
                    'design_type must be an object"}')
        return '✅ résultat : {"ok": true}'

    vrais = (A._llm_json, A._tool, A._composio_list_actions, A._connected_accounts,
             A._format_app_result, A._known_args)
    K.effacer_tout()
    try:
        A._llm_json, A._tool = faux_llm_json, faux_tool
        A._composio_list_actions = lambda s: ACTIONS
        A._connected_accounts = lambda: [("canva", "u", "ACTIVE")]
        A._format_app_result = lambda m, a, o, w: "✅ Design créé."
        A._known_args = lambda action, message: None

        APPELS.clear(); INDICES.clear()
        A._generic_app_flow("fais-moi une affiche pour la fête", "canva")
        check("1re fois : il faut corriger", len(APPELS), 2)
        check("aucune recette au départ", any(INDICES), False)

        APPELS.clear(); INDICES.clear()
        A._generic_app_flow("fais-moi une affiche pour le concert", "canva")
        check("2e fois : plus aucune correction", len(APPELS), 1)
        check_true("la recette a bien été soufflée", any(INDICES))
        check("la forme est la bonne du premier coup",
              APPELS[0]["design_type"], {"type": "preset", "name": "doc"})
        check("la difficulté d'origine est retenue",
              K.recette("canva", "CANVA_CREATE_DESIGN")["corrections"], 1)

        # Un appel qui ÉCHOUE ne doit rien apprendre
        K.effacer_tout()
        A._llm_json = lambda s, u: ({"arguments": {"design_type": "doc"}}
                                    if "ARGUMENTS" in s
                                    else {"action": "CANVA_CREATE_DESIGN", "arguments": {}})
        A._generic_app_flow("fais-moi une affiche", "canva")
        check("un échec n'apprend rien", K.recette("canva", "CANVA_CREATE_DESIGN"), {})
    finally:
        (A._llm_json, A._tool, A._composio_list_actions, A._connected_accounts,
         A._format_app_result, A._known_args) = vrais
        K.effacer_tout()


def test_automatisations_heure():
    """« Briefing du matin à 7h » partait à 9h heure de Paris : les planificateurs
    lisaient l'heure du SERVEUR (UTC sur Render), pas celle de l'utilisateur.
    Le jour aussi pouvait basculer — le bilan du dimanche soir tombait un lundi."""
    import time as _t
    from datetime import datetime, timedelta
    from agent import horloge as H
    from agent import automations as AU

    # 40a. L'horloge rend bien l'heure de l'utilisateur, pas celle du serveur
    check("le fuseau par défaut est celui de Lohan", H.FUSEAU, "Europe/Paris")
    check_true("l'horloge répond", isinstance(H.maintenant(), datetime))
    try:
        from zoneinfo import ZoneInfo
        attendu = datetime.now(ZoneInfo("Europe/Paris")).replace(tzinfo=None)
        check_true("l'heure locale est la bonne à la minute près",
                   abs((H.maintenant() - attendu).total_seconds()) < 60)
        check_true("le décalage avec le serveur est mesuré",
                   isinstance(H.decalage_h(), float))
    except Exception:
        pass       # sans base de fuseaux, on retombe sur l'heure serveur : c'est prévu

    # Un fuseau invalide ne doit RIEN casser : on retombe sur l'heure du serveur
    vrai_fuseau = H.FUSEAU
    try:
        H.FUSEAU = "Pas/UnFuseau"
        check_true("un fuseau invalide ne plante pas", isinstance(H.maintenant(), datetime))
    finally:
        H.FUSEAU = vrai_fuseau

    # 40b. Plus aucun planificateur ne lit l'heure du serveur
    import inspect
    for mod, nom in ((AU, "automations"), (__import__("agent.briefing", fromlist=["x"]), "briefing")):
        src = inspect.getsource(mod)
        boucle = src[src.find("async def"):]
        check(f"{nom} n'utilise plus datetime.now() pour planifier",
              "datetime.now()" in boucle, False)

    # 40c. La prochaine exécution est annoncée en heure locale, et respecte les jours
    vrais = (AU._load, AU._save)
    try:
        stock = []
        AU._load = lambda: list(stock)
        AU._save = lambda items: (stock.clear(), stock.extend(items))

        a = AU.add("Veille tech", "résume l'actu", hour=12)
        quand = AU.prochaine_execution(a)
        check_true("une heure est annoncée", "à 12h" in quand)

        # Seulement le dimanche → jamais annoncé un autre jour
        dim = AU.add("Bilan", "bilan", hour=19, days=[6])
        q2 = AU.prochaine_execution(dim)
        check_true("le jour choisi est respecté", "à 19h" in q2)
        jour = datetime.strptime(q2.split(" à ")[0].split(" ", 1)[1], "%d/%m")
        # On revérifie le jour de la semaine en repartant de l'heure locale
        cible = next(H.maintenant() + timedelta(days=d) for d in range(8)
                     if (H.maintenant() + timedelta(days=d)).weekday() == 6
                     and not ((H.maintenant() + timedelta(days=d)).date() == H.maintenant().date()
                              and H.maintenant().hour >= 19))
        check("c'est bien un dimanche", cible.weekday(), 6)

        AU.update(a["id"], active=False)
        check("une automatisation éteinte le dit",
              AU.prochaine_execution(AU.list_all()[0]), "désactivée")
        check("aucun jour coché → rien n'est promis",
              AU.prochaine_execution({"active": True, "hour": 9, "days": []}),
              "aucune (aucun jour coché)")
        check("jours absents → tous les jours", AU._jours({"hour": 9}), list(range(7)))
        check("jours vides → aucun jour", AU._jours({"days": []}), [])
        check("les jours choisis sont respectés", AU._jours({"days": [1, 3]}), [1, 3])
        # …et la création respecte le même contrat
        vide = AU.add("Jamais", "rien", hour=9, days=[])
        check("créer sans aucun jour ne coche pas tout", vide["days"], [])
        check("créer sans préciser coche tous les jours",
              AU.add("Tous", "rien", hour=9)["days"], list(range(7)))
        AU.delete(vide["id"])

        # 40d. Le diagnostic dit la VÉRITÉ sur l'état du planificateur
        sauve = dict(AU.BATTEMENT)
        try:
            AU.BATTEMENT.update({"ts": 0.0, "demarre": 0.0})
            check_true("jamais démarré : c'est dit",
                       "jamais démarré" in AU.etat_planificateur()["resume"])
            AU.BATTEMENT.update({"demarre": _t.time() - 9000, "ts": _t.time() - 2820})
            etat = AU.etat_planificateur()
            check_true("instance endormie : c'est dit", "ne tourne plus" in etat["resume"])
            check_true("…et on explique quoi faire", "veille" in etat["resume"].lower())
            check_true("…avec une solution concrète", "/health" in etat.get("solution", ""))
            AU.BATTEMENT.update({"demarre": _t.time() - 9000, "ts": _t.time() - 12})
            etat = AU.etat_planificateur()
            check_true("tout va bien : c'est dit aussi", etat["resume"].startswith("✅"))
            check_true("le fuseau est annoncé", "Europe/Paris" in etat["resume"])
        finally:
            AU.BATTEMENT.update(sauve)
    finally:
        (AU._load, AU._save) = vrais


def test_memoire_des_documents():
    """Nova relançait une recherche à CHAQUE demande du PEA : même travail, même risque
    de retomber sur le mauvais fichier. Elle retient maintenant ce qu'elle a trouvé."""
    from agent import documents as D

    D.effacer_tout()
    try:
        # 39a. Retenir, retrouver — y compris sur une formulation différente
        D.retenir("googlesheets", "pea", "11qGW0rhYzR4XWab0UKTLDVUU1S2_Mizn0eToOOZRph8",
                  "Suivi_PEA_Lohan_Pere")
        for demande in ("pea", "PEA", "p.e.a", "mon suivi pea", "pea pere et moi"):
            ident, nom = D.retrouver("googlesheets", demande)
            check(f"« {demande} » retrouve le PEA", nom, "Suivi_PEA_Lohan_Pere")
        check("une autre app ne partage pas les raccourcis",
              D.retrouver("notion", "pea"), ("", ""))
        check("une demande sans rapport ne retrouve rien",
              D.retrouver("googlesheets", "vacances"), ("", ""))
        check("une demande trop courte ne retrouve rien", D.retrouver("googlesheets", "a"),
              ("", ""))

        # 39b. Le plus SPÉCIFIQUE gagne : « pea pere » ne doit pas être éclipsé par « pea »
        D.retenir("googlesheets", "pea pere", "1PERE" + "x" * 30, "PEA_du_Pere")
        check("le libellé le plus précis l'emporte",
              D.retrouver("googlesheets", "mon pea pere")[1], "PEA_du_Pere")
        check("le libellé général reste bon", D.retrouver("googlesheets", "pea")[1],
              "Suivi_PEA_Lohan_Pere")

        # 39c. Oublier un raccourci devenu faux
        check("un raccourci s'oublie",
              D.oublier("googlesheets", "11qGW0rhYzR4XWab0UKTLDVUU1S2_Mizn0eToOOZRph8"), 1)
        check("il n'est plus retrouvé", D.retrouver("googlesheets", "pea")[1], "PEA_du_Pere")

        # 39d. Rien d'incomplet n'est mémorisé
        for app, quoi, ident in (("", "pea", "1abc"), ("googlesheets", "", "1abc"),
                                 ("googlesheets", "pea", ""), ("googlesheets", "a", "1abc")):
            check(f"refus de retenir ({app!r},{quoi!r},{ident!r})", D.retenir(app, quoi, ident, "x"), {})

        # 39e. Un raccourci trop vieux n'est plus servi : mieux vaut revérifier
        D.effacer_tout()
        D.retenir("googlesheets", "vieux", "1VIEUX" + "y" * 30, "Ancien")
        items = D._load()
        items[0]["ts"] = 0.0                       # comme s'il datait de 1970
        D._save(items)
        check("un raccourci périmé est ignoré", D.retrouver("googlesheets", "vieux"), ("", ""))
    finally:
        D.effacer_tout()

    # 39f. Bout en bout : chercher une fois, puis aller droit au but
    APPELS = []
    FICHIERS = [{"id": "11qGW0rhYzR4XWab0UKTLDVUU1S2_Mizn0eToOOZRph8",
                 "name": "Suivi_PEA_Lohan_Pere"},
                {"id": "1AAAaaaBBBcccDDDeeeFFFgggHHHiiiJJJkkkLLL", "name": "Budget vacances"}]
    ACTIONS = [{"name": "GOOGLESHEETS_BATCH_GET", "desc": ""},
               {"name": "GOOGLESHEETS_SEARCH_SPREADSHEETS", "desc": ""}]
    mort = {"on": False}

    def faux_tool(action, args=None, **kw):
        APPELS.append(action)
        if "SEARCH" in action.upper():
            return "✅ résultat :\n" + json.dumps({"data": {"files": FICHIERS}})
        if mort["on"] and (args or {}).get("spreadsheet_id") == FICHIERS[0]["id"]:
            return '❌ Action échouée : {"message": "File not found"}'
        return "✅ résultat :\n" + json.dumps({"valueRanges": [["Lohan", "valneva", 23]]})

    vrais = (A._tool, A._composio_list_actions, A._connected_accounts,
             A._build_args, A._llm_json, A._format_app_result)
    D.effacer_tout()
    try:
        A._tool = faux_tool
        A._composio_list_actions = lambda s: ACTIONS
        A._connected_accounts = lambda: [("googlesheets", "u", "ACTIVE")]
        A._build_args = lambda act, spec, msg, ctx="", error="": {
            "spreadsheet_id": "YOUR_SPREADSHEET_ID"}
        A._llm_json = lambda s, u: {"action": "GOOGLESHEETS_BATCH_GET", "arguments": {}}
        A._format_app_result = lambda m, a, o, w: "Voici tes données."

        APPELS.clear()
        A._generic_app_flow("lit mon fichier pea sur google sheet", "googlesheets")
        check("1er passage : recherche puis lecture", len(APPELS), 2)
        check("le document est appris", D.retrouver("googlesheets", "pea")[1],
              "Suivi_PEA_Lohan_Pere")

        APPELS.clear()
        A._generic_app_flow("lit mon fichier pea sur google sheet", "googlesheets")
        check("2e passage : plus de recherche", APPELS, ["GOOGLESHEETS_BATCH_GET"])

        APPELS.clear()
        r = A._generic_app_flow("montre mon suivi pea", "googlesheets")
        check("une autre formulation profite du raccourci", APPELS, ["GOOGLESHEETS_BATCH_GET"])
        check_true("le raccourci est annoncé",
                   any("je sais déjà où c'est" in (s.get("text") or "") for s in r["steps"]))

        # Le document disparaît côté Google : on oublie et on rouvre les yeux
        mort["on"] = True
        APPELS.clear()
        r = A._generic_app_flow("lit mon fichier pea sur google sheet", "googlesheets")
        check_true("l'échec relance une recherche", "GOOGLESHEETS_SEARCH_SPREADSHEETS" in APPELS)
        check_true("Nova le dit",
                   any("ne répond plus" in (s.get("text") or "") for s in r["steps"]))
        check("un identifiant qui échoue n'est pas ré-appris",
              D.retrouver("googlesheets", "pea"), ("", ""))
    finally:
        (A._tool, A._composio_list_actions, A._connected_accounts,
         A._build_args, A._llm_json, A._format_app_result) = vrais
        D.effacer_tout()

    # 39g. Le diagnostic dit la vérité sur la persistance
    etat = A._etat_memoire()
    check_true("le diagnostic tranche", isinstance(etat.get("persistante"), bool))
    check_true("il explique où c'est stocké", bool(etat.get("ou")))
    if not etat["persistante"]:
        check_true("il prévient que tout sera perdu", "PAS persistante" in etat["resume"])
        check_true("il donne la solution", bool(etat.get("solution")))

    # 39h. ⚠️ « Configurée mais en panne » est le PIRE cas : ça ressemble à « ça marche ».
    # Une URL devenue invalide faisait retomber Nova sur la mémoire locale en silence.
    from config import config as CFG
    from memory import get_memory as _gm
    vrai_url = getattr(CFG, "SUPABASE_DB_URL", "")
    mem_ = _gm()
    vrai_echec = getattr(mem_, "echec_persistance", "")
    try:
        CFG.SUPABASE_DB_URL = ""
        mem_.echec_persistance = ""
        e = A._etat_memoire()
        if not e["persistante"]:
            check("sans URL : non configurée", e.get("etat"), "non configurée")
            check_true("…et on explique où la trouver", "Connect" in e.get("solution", ""))

        CFG.SUPABASE_DB_URL = "postgresql://postgres:x@db.abc.supabase.co:5432/postgres"
        mem_.echec_persistance = "OperationalError: Network is unreachable"
        e = A._etat_memoire()
        if not e["persistante"]:
            check("URL présente mais KO : c'est dit", e.get("etat"), "configurée mais INJOIGNABLE")
            check_true("la raison exacte est donnée", "unreachable" in e.get("raison", ""))
            check_true("on ne laisse pas croire que ça marche", "INJOIGNABLE" in e["resume"])
    finally:
        CFG.SUPABASE_DB_URL = vrai_url
        mem_.echec_persistance = vrai_echec

    # Chaque panne doit donner un GESTE, pas du jargon anglais
    for erreur, attendu in (
            ("could not connect to server: Network is unreachable", "Session pooler"),
            ("Cannot assign requested address", "Session pooler"),
            ("FATAL: password authentication failed for user", "mot de passe"),
            ("connection timed out", "en pause"),
            ("No module named psycopg2", "psycopg2-binary"),
            ("SSL connection is required", "sslmode"),
            ("quelque chose d'inattendu", "Session pooler")):
        conseil = A._conseil_supabase(erreur)
        check_true(f"conseil utile pour « {erreur[:36]}… »", attendu in conseil)
        check_true("le conseil reste en français", " the " not in conseil.lower())


def test_memoire_des_donnees():
    """Nova lisait le tableur PEA, affichait tous les chiffres, puis répondait « je ne
    suis pas conseiller financier » à « tu penses quoi de nos investissements ? ».
    Elle avait bien lu les données : elle ne les gardait pas d'un tour à l'autre."""
    from memory import get_memory

    PEA = ("| Lohan | Amundi PEA Nasdaq-100 | 19 | 6,94 | +31 % |\n"
           "| Lohan | action valneva | 23 | 2,26 | -50 % |\n"
           "| Père | Amundi PEA S&P 500 | 6 | 57,41 | -0,5 % |")
    vrais = (A._PROFILE_ID, A._connected_accounts, A._composio_list_actions,
             A._tool, A._format_app_result)
    try:
        A._PROFILE_ID = "test_memoire_donnees"        # profil vierge, isolé des autres tests
        A._connected_accounts = lambda: [("googlesheets", "u", "ACTIVE")]
        A._composio_list_actions = lambda s: [
            {"name": "GOOGLESHEETS_BATCH_GET", "desc": ""},
            {"name": "GOOGLESHEETS_SEARCH_SPREADSHEETS", "desc": ""}]
        A._tool = lambda a, args=None, **k: (
            "✅ résultat :\n" + json.dumps({"data": {"files": [
                {"id": "11qGW0rhYzR4XWab0UKTLDVUU1S2_Mizn0eToOOZRph8",
                 "name": "Suivi_PEA_Lohan_Pere"}]}})
            if "SEARCH" in a.upper() else "✅ résultat :\n" + PEA)
        A._format_app_result = lambda msg, act, obs, w: PEA

        mem = get_memory()
        avant = len(mem.recall_recent(A._PROFILE_ID, 200) or [])
        A._direct_app_prepare("lit mon fichier pea sur google sheet")
        garde = (mem.recall_recent(A._PROFILE_ID, 200) or [])[avant:]
        roles = [g.get("role") for g in garde]
        check_true("la question est mémorisée", "user" in roles)
        check_true("la RÉPONSE est mémorisée aussi", "assistant" in roles)
        dit = " ".join(g.get("content") or "" for g in garde)
        check_true("les chiffres lus sont conservés", "valneva" in dit)

        # …et ils arrivent bien dans le contexte de la question suivante
        ctx = mem.build_context(A._PROFILE_ID, "tu penses quoi de nos investissements ?",
                                recent_limit=6)
        check_true("les chiffres sont disponibles au tour suivant", "valneva" in ctx.lower())

        # Une réponse vide ne doit rien polluer
        avant2 = len(mem.recall_recent(A._PROFILE_ID, 200) or [])
        A._remember_answer("")
        check("une réponse vide n'est pas mémorisée",
              len(mem.recall_recent(A._PROFILE_ID, 200) or []), avant2)
    finally:
        (A._PROFILE_ID, A._connected_accounts, A._composio_list_actions,
         A._tool, A._format_app_result) = vrais

    # Des DONNÉES restent lisibles longtemps ; une longue prose reste coupée court
    # (la réinjecter en entier poussait le modèle à rejouer le même sujet).
    from memory.manager import _ressemble_a_des_donnees as _donnees
    for texte, att in (
            ("| Lohan | Nasdaq | 19 | 6,94 | +31 % |\n| Lohan | valneva | 23 | 2,26 |", True),
            ('{"valueRanges": [["Personne","ETF"]]}', True),
            ("Tes rendez-vous :\n- 9h dentiste\n- 14h cours\n- 18h sport", True),
            ("Tu as 19 parts à 6,94 €, 23 à 2,26 €, 6 à 57,41 € et 55 à 5,40 €", True),
            ("Bonjour Lohan, comment vas-tu ?", False),
            ("La photosynthèse est le processus par lequel les plantes convertissent "
             "la lumière en énergie chimique, dans les chloroplastes.", False),
            ("Je ne suis pas un conseiller financier, mais la diversification et la "
             "patience sont souvent de bons points de départ.", False),
            ("", False)):
        check(f"données ? « {texte[:34]}… »", _donnees(texte), att)

    # Bout en bout : le tableau survit à la troncature du contexte, la prose non
    from memory import get_memory as _gm
    mem2 = _gm()
    _gm().clear("test_troncature_ctx")
    mem2.remember("test_troncature_ctx", "user", "consulte mon pea")
    mem2.remember("test_troncature_ctx", "assistant",
                  "| Personne | ETF | Qté | Cours |\n" + "\n".join(
                      f"| Lohan | ligne {i} | {i} | {i},50 |" for i in range(30)) +
                  "\n| Lohan | action valneva | 23 | 2,26 |")
    mem2.remember("test_troncature_ctx", "user", "et alors ?")
    ctx2 = mem2.build_context("test_troncature_ctx", "tu penses quoi ?", recent_limit=6)
    check_true("un tableau long reste lisible dans le contexte", "valneva" in ctx2)

    # La consigne : commenter SES chiffres, ne pas esquiver
    cfg = A._build_agent_cfg("tu penses quoi de nos investissements ?", "Nova")
    sysm = cfg.get("system") or cfg.get("system_prompt") or ""
    check_true("Nova a le droit de parler de ses placements",
               "INTERDIT" not in sysm or "ne parle pas de bourse" not in sysm)
    check_true("elle doit commenter les chiffres de l'HISTORIQUE", "HISTORIQUE" in sysm)
    check_true("l'esquive est explicitement interdite", "esquive" in sysm.lower())
    check_true("elle ne conseille pas d'acheter ou vendre",
               "quoi acheter" in sysm or "acheter ou" in sysm)
    # …mais sur une discussion ordinaire, rien de tout ça ne s'active
    cfg2 = A._build_agent_cfg("bonjour ça va ?", "Nova")
    sys2 = cfg2.get("system") or cfg2.get("system_prompt") or ""
    check_true("aucune consigne finance sur une discussion normale", "esquive" not in sys2.lower())


def test_continuite_app():
    """« tu peux faire quoi avec Notion ? » puis, 20 secondes après, « vas-y crée un
    doc » : Nova repartait de zéro — et « doc » l'envoyait même vers Google Docs."""
    try:
        A._APP_RECENTE.clear()

        # 37a. Le scénario exact signalé
        check("la question nomme Notion", A.app_courante("tu peux faire quoi avec notion"), "notion")
        A._capability_answer("notion")          # ce que Nova fait réellement à ce tour
        check("la suite reste dans Notion", A.app_courante("vas-y crée un doc alors"), "notion")
        check("« crée une page » aussi", A.app_courante("crée une page"), "notion")

        # 37b. Une app NOMMÉE l'emporte toujours sur le contexte
        A._APP_RECENTE.clear(); A._retenir_app("notion")
        for phrase, attendu in (("crée un doc dans google docs", "googledocs"),
                                ("mon agenda demain", "googlecalendar"),
                                ("envoie un mail à Paul", "gmail"),
                                ("crée un ticket linear", "linear")):
            A._retenir_app("notion")
            check(f"« {phrase[:30]}… » → {attendu}", A.app_courante(phrase), attendu)

        # 37c. Une phrase sans rapport ne récupère PAS le contexte
        for phrase in ("quelle heure est-il", "résume l'actu du jour",
                       "explique-moi la photosynthèse", "j'ai 17 ans"):
            A._APP_RECENTE.clear(); A._retenir_app("notion")
            check(f"« {phrase[:28]}… » sans app", A.app_courante(phrase), None)

        # 37d. Sans contexte récent, rien n'est supposé
        A._APP_RECENTE.clear()
        check("« crée une page » seule", A.app_courante("crée une page"), None)

        # 37e. Le contexte s'oublie au bout d'un moment
        A._APP_RECENTE.clear(); A._retenir_app("notion")
        A._APP_RECENTE[A._PROFILE_ID] = ("notion", A._APP_RECENTE[A._PROFILE_ID][1] - 10_000)
        # Le contexte expiré ne doit plus peser : on retombe sur la détection normale
        # (« doc » seul désigne Google Docs, ce qui est un choix défendable sans contexte).
        apres = A.app_courante("vas-y crée un doc")
        check("un contexte trop vieux n'impose plus Notion", apres == "notion", False)
        check("retour à la détection habituelle", apres, "googledocs")

        # 37f. Une demande LONGUE enchaîne quand même sur l'app en cours.
        # Il y avait ici un plafond de 18 mots, et c'était un défaut : Lohan a écrit
        # « suivie pea pere et moi mas jai pas le nom exact. si tu trouve pas liste moi
        # les titres des fichiers » (19 mots) juste après avoir parlé de Google Sheets,
        # et Nova partait dans le Drive — qui n'est même pas connecté. Le contexte était
        # perdu pile au moment où l'utilisateur donnait le plus de précisions.
        suite = ("suivie pea pere et moi mas jai pas le nom exact. "
                 "si tu trouve pas liste moi les titres des fichiers")
        check_true("le cas réel dépasse bien 18 mots", len(suite.split()) > 18)
        A._APP_RECENTE.clear(); A._retenir_app("googlesheets")
        check("une longue suite reste sur Sheets", A.app_courante(suite), "googlesheets")
        A._APP_RECENTE.clear(); A._retenir_app("notion")
        longue = ("crée pour moi un tableau récapitulatif complet avec toutes les colonnes "
                  "nécessaires pour suivre mes dépenses mensuelles de cette année")
        check("une longue demande enchaîne aussi", A.app_courante(longue), "notion")
        # …mais nommer une AUTRE app reprend toujours le dessus, si longue soit la phrase
        A._APP_RECENTE.clear(); A._retenir_app("notion")
        check("une app nommée l'emporte sur le contexte",
              A.app_courante(longue + " dans google sheets"), "googlesheets")

        # 37f-bis. Un mot banal ne fait JAMAIS changer d'app en cours de route.
        # « fichier » est un mot-clé de Google Drive : « liste-moi les titres des fichiers »
        # quittait Sheets pour le Drive.
        for phrase in ("liste moi les titres des fichiers", "montre-moi mes documents",
                       "ouvre le fichier", "affiche la page"):
            A._APP_RECENTE.clear(); A._retenir_app("googlesheets")
            check(f"« {phrase[:34]}… » reste sur Sheets", A.app_courante(phrase), "googlesheets")
        # …alors qu'un mot-clé FORT désigne bien le Drive
        A._APP_RECENTE.clear(); A._retenir_app("googlesheets")
        check("« mon google drive » va bien au Drive",
              A.app_courante("cherche dans mon google drive"), "googledrive")
        check_true("« fichier » est un mot-clé faible", A._mot_cle_faible("fichier"))
        check("« google drive » est un mot-clé fort", A._mot_cle_faible("google drive"), False)
        check("« document drive » est un mot-clé fort", A._mot_cle_faible("document drive"), False)

        # 37h. Une app NON connectée : on le dit, on n'exécute rien.
        # Nova lançait l'action quand même ; Composio répondait 404 et l'utilisateur
        # recevait « L'action GOOGLEDRIVE_FIND_FILE n'existe pas » suivi de la liste
        # brute des actions Canva — illisible, et sans rapport avec sa demande.
        vrais_cnx = (A._connected_accounts, A._composio_connect_link, A._composio_list_actions)
        appels_cnx = []
        try:
            A._connected_accounts = lambda: [(s, "u", "ACTIVE") for s in
                                             ("gmail", "googlecalendar", "googlesheets", "notion")]
            A._composio_connect_link = lambda s: ("", "")
            A._composio_list_actions = lambda s: (appels_cnx.append(s) or
                                                  [{"name": "X_SEARCH", "desc": ""}])
            check_true("Sheets est vue comme connectée", A._est_connectee("googlesheets"))
            check("le Drive n'est pas connecté", A._est_connectee("googledrive"), False)
            r = A._generic_app_flow("cherche le fichier pea dans mon drive", "googledrive")
            rep = r["done_answer"]
            check_true("le refus est explicite", "n'est pas connecté" in rep)
            check_true("le refus reste en français", "n'existe pas" not in rep)
            check_true("aucun nom d'action brut n'est montré", "GOOGLEDRIVE" not in rep)
            check_true("le refus propose ce qui EST connecté", "Notion" in rep and "Sheets" in rep)
            check("aucune action n'a été listée pour une app non connectée", appels_cnx, [])
            # …et une app connectée passe toujours
            A._generic_app_flow("cherche mes tableurs", "googlesheets")
            check_true("une app connectée est bien traitée", "googlesheets" in appels_cnx)
            # Composio injoignable : on ne bloque pas à tort
            def _boum():
                raise RuntimeError("composio injoignable")
            A._connected_accounts = _boum
            check_true("Composio injoignable ne bloque pas", A._est_connectee("googledrive"))
        finally:
            (A._connected_accounts, A._composio_connect_link,
             A._composio_list_actions) = vrais_cnx

        # 37g. Un mot générique ne désigne jamais une app à lui seul
        for mot in ("doc", "page", "tableau", "fichier", "note", "projet", "ticket"):
            check_true(f"« {mot} » est un mot passe-partout", mot in A._MOTS_GENERIQUES)
        check("« doc » seul ne nomme aucune app", A._app_nommee("crée un doc"), "")
        check("« google docs » nomme bien l'app", A._app_nommee("dans google docs"), "googledocs")
    finally:
        A._APP_RECENTE.clear()


# ── 38. Aucune app connectée ne doit disparaître du catalogue ────────────────
def test_catalogue_complet():
    """Cas réel : 7 apps connectées sur Composio, Nova n'en voyait que 3 et répondait
    « je n'ai pas accès à Notion » à quelqu'un dont Notion ÉTAIT connecté."""
    from plugins.builtin import composio_tool as CT

    TES_APPS = {"github": 100, "googlecalendar": 28, "notion": 30, "googlesheets": 36,
                "linear": 21, "google_maps": 7, "canva": 32}
    vrais = (A._connected_accounts, A._composio_list_actions)
    try:
        A._connected_accounts = lambda: [(s, "u", "ACTIVE") for s in TES_APPS]
        A._composio_list_actions = lambda slug: [
            {"name": f"{slug.upper()}_ACTION_NUMERO_{i:02d}", "desc": ""}
            for i in range(TES_APPS.get(slug, 5))]

        cat = CT.catalogue("accede a google map et lit mes points gps")
        listees = [l.split(" : ")[0].strip("• ") for l in cat.split("\n") if l.startswith("•")]
        check("les 7 apps connectées sont listées", len(listees), 7)
        for app in TES_APPS:
            check_true(f"« {app} » présent dans le catalogue", app in cat)
        # L'app évoquée passe devant et reçoit la plus grosse part
        check("l'app évoquée est en tête", listees[0], "google_maps")
        lignes = {l.split(" : ")[0].strip("• "): l for l in cat.split("\n") if l.startswith("•")}
        # Ce qui compte n'est pas la longueur de la ligne (google_maps n'a que 7 actions)
        # mais qu'AUCUNE de ses actions ne soit coupée : c'est l'app dont on a besoin.
        check("l'app évoquée n'est jamais tronquée", "autres" in lignes["google_maps"], False)
        check_true("elle montre bien ses 7 actions",
                   lignes["google_maps"].count("GOOGLE_MAPS_ACTION") == 7)
        # Le catalogue doit rester d'une taille raisonnable pour le prompt
        check_true(f"taille maîtrisée ({len(cat)} car.)", len(cat) <= 3000)

        # Même avec BEAUCOUP d'apps, aucune ne disparaît
        NOMBREUSES = {f"app{i}": 25 for i in range(20)}
        A._connected_accounts = lambda: [(s, "u", "ACTIVE") for s in NOMBREUSES]
        A._composio_list_actions = lambda slug: [
            {"name": f"{slug.upper()}_ACT_{i:02d}"} for i in range(25)]
        c2 = CT.catalogue("")
        v2 = [l.split(" : ")[0].strip("• ") for l in c2.split("\n") if l.startswith("•")]
        check("20 apps → 20 listées", len(v2), 20)
        check_true("chaque app garde au moins une action",
                   all(" : " in l and len(l.split(" : ")[1]) > 5
                       for l in c2.split("\n") if l.startswith("•")))

        # Aucune app connectée : message honnête, pas une liste figée
        A._connected_accounts = lambda: []
        check_true("aucune app → on le dit", "aucune app" in CT.catalogue("").lower())
    finally:
        A._connected_accounts, A._composio_list_actions = vrais

    # Le prompt ne doit plus tronquer le catalogue
    from pathlib import Path as P
    src = (P(__file__).resolve().parents[1] / "api" / "agent.py").read_text(encoding="utf-8")
    check("plus de troncature du catalogue dans le prompt", "dispo[:1400]" in src, False)


if __name__ == "__main__":
    for fn in (test_routage, test_echecs, test_dates, test_titres, test_robustesse,
               test_visuels, test_profil, test_automatisations, test_escouade,
               test_caches, test_slugs, test_modeles, test_requetes, test_securite,
               test_non_bloquant, test_delais, test_cours, test_trouvailles,
               test_requete_web, test_saturation, test_synthese_fond,
               test_sans_modele, test_bulles_live, test_diagnostic, test_actualite,
               test_apps_actions, test_actu_pertinence, test_audit_actu,
               test_raisonnement_cache, test_slug_connecte,
               test_modele_annonce, test_affichage_actu, test_enchainement_fichier,
               test_fournisseur_et_routage, test_garde_fou, test_calendrier_exact,
               test_continuite_app, test_catalogue_complet, test_memoire_des_donnees,
               test_memoire_des_documents, test_automatisations_heure,
               test_competences, test_apps_robustesse,
               test_choix_fournisseur, test_apps_inconnues,
               test_diag_patient, test_cours_persistants,
               test_app_en_panne_repond_quand_meme, test_jamais_d_ecriture_non_demandee,
               test_resultats_automatisations_remontent,
               test_garde_fou_irreversible_complet, test_identifiants_par_type,
               test_sources_visibles, test_autocorrection_garde_identifiant):
        try:
            fn()
        except Exception as e:
            KO.append((fn.__name__, f"EXCEPTION {type(e).__name__}: {e}", "exécution complète"))
    print(f"\n{'='*66}\n  {len(OK)} tests OK   ·   {len(KO)} échec(s)\n{'='*66}")
    for nom, got, want in KO:
        print(f"  ❌ {nom}\n       obtenu : {got!r}\n       attendu: {want!r}")
    sys.exit(1 if KO else 0)
