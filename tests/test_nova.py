"""
Suite de tests Nova — vérifie les chemins critiques SANS réseau ni clés.

But : attraper les régressions avant le déploiement (routage, extraction, gestion d'erreur,
robustesse aux valeurs vides/None). Lancer avec :  python tests/test_nova.py
"""
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


if __name__ == "__main__":
    for fn in (test_routage, test_echecs, test_dates, test_titres, test_robustesse,
               test_visuels, test_profil, test_automatisations, test_escouade,
               test_caches, test_requetes, test_securite):
        try:
            fn()
        except Exception as e:
            KO.append((fn.__name__, f"EXCEPTION {type(e).__name__}: {e}", "exécution complète"))
    print(f"\n{'='*66}\n  {len(OK)} tests OK   ·   {len(KO)} échec(s)\n{'='*66}")
    for nom, got, want in KO:
        print(f"  ❌ {nom}\n       obtenu : {got!r}\n       attendu: {want!r}")
    sys.exit(1 if KO else 0)
