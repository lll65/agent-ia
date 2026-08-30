"""
Entrepôt — la façon SÛRE d'écrire ce que Nova doit retenir.

⚠️ LE DÉFAUT QUE CE MODULE SUPPRIME. Quatre modules (profil, compétences,
documents, automatisations) sauvegardaient ainsi :

    items = _load()          # connexion n°1
    ...                      # on modifie la liste
    DELETE FROM la_table     # connexion n°2
    INSERT × len(items)

Si la connexion n°1 échoue — un simple hoquet du pooler Supabase, un
démarrage à froid, les 10 s de connect_timeout dépassées, ce qui arrive
régulièrement sur l'offre gratuite —, `_sb()` avalait l'exception, `_load()`
retombait sur le fichier local (absent sur un conteneur Render fraîchement
déployé) et renvoyait []. Nova croyait la mémoire vide. Au fait suivant, la
connexion remarchait : le DELETE effaçait les 60 faits réels et réinsérait le
seul fait connu. Tout le profil disparaissait DÉFINITIVEMENT, en silence, et
la copie locale était écrasée dans la foulée.

Deux règles ici, et elles suffisent :
 1. `charge()` dit s'il a VRAIMENT lu la base. Un appelant qui s'apprête à
    supprimer doit refuser d'écrire quand la lecture n'était pas fiable.
 2. `ecrit()` ne reconstruit jamais la table : il met à jour les lignes
    fournies (INSERT ... ON CONFLICT) et ne supprime que les identifiants
    explicitement nommés. Une écriture ne peut donc plus rien perdre.
"""
import json
import logging
from pathlib import Path

from config import config

logger = logging.getLogger(__name__)


class Entrepot:
    """Une table Supabase (clé, data) doublée d'un fichier local de secours."""

    def __init__(self, table: str, fichier: str, cle: str = "id"):
        self.table = table
        self.fichier = Path(fichier)
        # Nom de la colonne clé ET du champ correspondant dans chaque élément.
        # Les tables existantes chez l'utilisateur ne s'accordent pas là-dessus
        # (« id » pour le profil, « cle » pour les documents) : on s'adapte plutôt
        # que de casser des données déjà en place.
        self.cle = cle

    # ── Connexion ─────────────────────────────────────────────────────────────
    def configure(self) -> bool:
        return bool(getattr(config, "SUPABASE_DB_URL", ""))

    def _conn(self):
        if not self.configure():
            return None
        try:
            import psycopg2
            conn = psycopg2.connect(config.SUPABASE_DB_URL, connect_timeout=10)
            conn.autocommit = True
            with conn.cursor() as c:
                c.execute(f"CREATE TABLE IF NOT EXISTS {self.table} "
                          f"({self.cle} text PRIMARY KEY, data jsonb)")
            return conn
        except Exception as e:
            logger.warning(f"[entrepot] {self.table} injoignable ({type(e).__name__}: {e}).")
            return None

    # ── Lecture ───────────────────────────────────────────────────────────────
    def charge(self) -> tuple[list, bool]:
        """(éléments, lecture fiable ?).

        « fiable » vaut False quand Supabase est configuré mais n'a pas répondu :
        la liste rendue est alors une copie locale possiblement vide ou périmée,
        et il ne faut RIEN supprimer sur cette base.
        """
        if self.configure():
            conn = self._conn()
            if conn:
                try:
                    with conn.cursor() as c:
                        # Sans ORDER BY l'ordre est arbitraire et change d'un
                        # redémarrage à l'autre — or plusieurs appelants tronquent
                        # la liste : ils jetteraient au hasard.
                        c.execute(f"SELECT data FROM {self.table} ORDER BY {self.cle}")
                        rows = c.fetchall()
                    conn.close()
                    return ([r[0] if isinstance(r[0], dict) else json.loads(r[0])
                             for r in rows], True)
                except Exception as e:
                    logger.warning(f"[entrepot] lecture {self.table} échouée "
                                   f"({type(e).__name__}) — écriture bloquée par sécurité.")
            return (self._local(), False)
        return (self._local(), True)

    def _local(self) -> list:
        if self.fichier.exists():
            try:
                data = json.loads(self.fichier.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []
            except Exception:
                return []
        return []

    # ── Écriture ──────────────────────────────────────────────────────────────
    def ecrit(self, items: list, supprimes=()) -> bool:
        """Met à jour les éléments fournis et supprime les identifiants nommés.

        Jamais de DELETE global : une panne au mauvais moment ne peut plus
        transformer une liste incomplète en vérité.
        """
        ok_local = False
        try:
            self.fichier.parent.mkdir(parents=True, exist_ok=True)
            self.fichier.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
            ok_local = True
        except Exception as e:
            logger.warning(f"[entrepot] écriture locale {self.fichier} échouée ({e}).")
        if not self.configure():
            return ok_local
        conn = self._conn()
        if not conn:
            return ok_local
        try:
            with conn.cursor() as c:
                for it in items:
                    ident = str(it.get(self.cle) or "")
                    if not ident:
                        continue
                    c.execute(
                        f"INSERT INTO {self.table} ({self.cle}, data) VALUES (%s, %s) "
                        f"ON CONFLICT ({self.cle}) DO UPDATE SET data = EXCLUDED.data",
                        (ident, json.dumps(it, ensure_ascii=False)))
                for ident in supprimes:
                    c.execute(f"DELETE FROM {self.table} WHERE {self.cle} = %s", (str(ident),))
            conn.close()
            return True
        except Exception as e:
            logger.warning(f"[entrepot] écriture {self.table} échouée ({type(e).__name__}: {e}).")
            try:
                conn.close()
            except Exception:
                pass
            return ok_local

    def ecrit_un(self, item: dict) -> bool:
        """Ajoute ou remplace UN élément, sans jamais toucher aux autres.

        C'est la forme à préférer : elle ne dépend pas d'une lecture préalable,
        donc une base injoignable ne peut ni faire perdre ni faire oublier quoi
        que ce soit. Le fichier local est fusionné, pas réécrit à l'aveugle.
        """
        ident = str(item.get(self.cle) or "")
        if not ident:
            return False
        local = [x for x in self._local() if str(x.get(self.cle) or "") != ident]
        local.append(item)
        return self.ecrit(local)

    def supprime(self, ids) -> bool:
        """Retire des éléments nommément — jamais « tout ce qui n'est pas dans ma liste »."""
        ids = {str(i) for i in ids if i}
        if not ids:
            return True
        local = [x for x in self._local() if str(x.get(self.cle) or "") not in ids]
        return self.ecrit(local, supprimes=ids)

    def vide(self) -> bool:
        """Tout effacer — uniquement sur demande EXPLICITE de l'utilisateur."""
        try:
            self.fichier.parent.mkdir(parents=True, exist_ok=True)
            self.fichier.write_text("[]", encoding="utf-8")
        except Exception:
            pass
        conn = self._conn()
        if not conn:
            return not self.configure()
        try:
            with conn.cursor() as c:
                c.execute(f"DELETE FROM {self.table}")
            conn.close()
            return True
        except Exception:
            return False
