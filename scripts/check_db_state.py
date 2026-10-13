#!/usr/bin/env python3
"""Analyse complète de l'état de la base de données."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.disable(logging.INFO)

from sqlalchemy import text

from src.db.models import get_engine


def pct(n, total):
    return f"{100 * n / max(total, 1):.1f}%"


def main():
    engine = get_engine()

    with engine.connect() as c:
        def q(sql, params=None):
            return c.execute(text(sql), params or {}).scalar()

        def qall(sql, params=None):
            return c.execute(text(sql), params or {}).fetchall()

        print("=" * 60)
        print("ÉTAT DE LA BASE PG - analyse complète")
        print("=" * 60)

        # 1. Lois
        total_lois = q("SELECT count(*) FROM lois")
        lf = q("SELECT count(*) FROM lois WHERE type='LF'")
        lfr = q("SELECT count(*) FROM lois WHERE type='LFR'")
        lfss = q("SELECT count(*) FROM lois WHERE type='LFSS'")
        min_d = q("SELECT min(date_promulgation) FROM lois")
        max_d = q("SELECT max(date_promulgation) FROM lois")
        print(f"\n--- LOIS: {total_lois} ({min_d} → {max_d}) ---")
        print(f"  LF: {lf} | LFR: {lfr} | LFSS: {lfss}")

        # 2. Articles
        total_art = q("SELECT count(*) FROM articles")
        art_no_contenu = q("SELECT count(*) FROM articles WHERE contenu_texte IS NULL OR contenu_texte = ''")
        print(f"\n--- ARTICLES: {total_art} ---")
        print(f"  Sans contenu: {art_no_contenu} ({pct(art_no_contenu, total_art)})")

        # 3. Dossiers
        total_dos = q("SELECT count(*) FROM dossiers_legislatifs")
        an_dos = q("SELECT count(*) FROM dossiers_legislatifs WHERE chambre='AN'")
        senat_dos = q("SELECT count(*) FROM dossiers_legislatifs WHERE chambre='SENAT'")
        lois_sans_an = q("""
            SELECT count(*) FROM lois l
            WHERE NOT EXISTS (SELECT 1 FROM dossiers_legislatifs d WHERE d.loi_id = l.id AND d.chambre = 'AN')
        """)
        lois_sans_senat = q("""
            SELECT count(*) FROM lois l
            WHERE NOT EXISTS (SELECT 1 FROM dossiers_legislatifs d WHERE d.loi_id = l.id AND d.chambre = 'SENAT')
        """)
        print(f"\n--- DOSSIERS: {total_dos} (AN: {an_dos}, Sénat: {senat_dos}) ---")
        print(f"  Lois sans dossier AN: {lois_sans_an}")
        print(f"  Lois sans dossier Sénat: {lois_sans_senat}")

        # 4. Débats
        total_deb = q("SELECT count(*) FROM debats")
        an_deb = q("SELECT count(*) FROM debats WHERE chambre='AN'")
        senat_deb = q("SELECT count(*) FROM debats WHERE chambre='SENAT'")
        orphan_deb = q("SELECT count(*) FROM debats WHERE dossier_id IS NULL")
        no_ci = q("SELECT count(*) FROM debats WHERE contenu_integral IS NULL OR contenu_integral = ''")
        print(f"\n--- DÉBATS: {total_deb:,} (AN: {an_deb:,}, Sénat: {senat_deb:,}) ---")
        print(f"  Orphelins: {orphan_deb}")
        print(f"  Sans contenu_integral: {no_ci:,} ({pct(no_ci, total_deb)})")

        # Débats par loi - les plus faibles
        print(f"\n  Couverture par loi (15 plus faibles):")
        rows = qall("""
            SELECT * FROM (
                SELECT l.numero, l.type, l.annee,
                    (SELECT count(*) FROM debats db JOIN dossiers_legislatifs dl ON db.dossier_id = dl.id
                     WHERE dl.loi_id = l.id AND db.chambre = 'AN') as cnt_an,
                    (SELECT count(*) FROM debats db JOIN dossiers_legislatifs dl ON db.dossier_id = dl.id
                     WHERE dl.loi_id = l.id AND db.chambre = 'SENAT') as cnt_senat
                FROM lois l
            ) sub
            ORDER BY cnt_an + cnt_senat ASC LIMIT 15
        """)
        for num, typ, annee, cnt_an, cnt_senat in rows:
            flag = " !!!" if cnt_an + cnt_senat == 0 else (" !" if cnt_an + cnt_senat < 5 else "")
            print(f"    {num:15s} {typ:4s} {annee}: AN={cnt_an:3d}  Sénat={cnt_senat:3d}{flag}")

        # Débats sans intervention
        deb_no_int = q("""
            SELECT count(*) FROM debats d
            WHERE NOT EXISTS (SELECT 1 FROM interventions i WHERE i.debat_id = d.id)
        """)
        print(f"  Débats sans intervention: {deb_no_int:,}")

        # 5. Interventions
        total_int = q("SELECT count(*) FROM interventions")
        null_am = q("SELECT count(*) FROM interventions WHERE articles_mentionnes IS NULL")
        with_am = q("SELECT count(*) FROM interventions WHERE articles_mentionnes IS NOT NULL AND articles_mentionnes::text != '[]'")
        no_contenu_int = q("SELECT count(*) FROM interventions WHERE contenu IS NULL OR contenu = ''")
        avg_len = q("SELECT avg(length(contenu)) FROM interventions WHERE contenu IS NOT NULL")
        usable_int = q("SELECT count(*) FROM interventions WHERE length(contenu) > 50")
        print(f"\n--- INTERVENTIONS: {total_int:,} ---")
        print(f"  articles_mentionnes NULL: {null_am:,}")
        print(f"  Avec mentions: {with_am:,} ({pct(with_am, total_int)})")
        print(f"  Sans contenu: {no_contenu_int:,}")
        print(f"  Exploitables (>50 chars): {usable_int:,} ({pct(usable_int, total_int)})")
        print(f"  Longueur moyenne: {avg_len:.0f} chars")

        # 6. Amendements
        total_amend = q("SELECT count(*) FROM amendements")
        no_art = q("SELECT count(*) FROM amendements WHERE article_id IS NULL")
        no_dos = q("SELECT count(*) FROM amendements WHERE dossier_id IS NULL")
        no_contenu_am = q("SELECT count(*) FROM amendements WHERE contenu IS NULL OR contenu = ''")
        no_expose = q("SELECT count(*) FROM amendements WHERE expose_motifs IS NULL OR expose_motifs = ''")
        adopte = q("SELECT count(*) FROM amendements WHERE sort = 'adopte'")
        print(f"\n--- AMENDEMENTS: {total_amend:,} ---")
        print(f"  Sans article_id: {no_art:,} ({pct(no_art, total_amend)})")
        print(f"  Sans dossier_id: {no_dos:,}")
        print(f"  Sans contenu: {no_contenu_am:,} ({pct(no_contenu_am, total_amend)})")
        print(f"  Sans exposé motifs: {no_expose:,} ({pct(no_expose, total_amend)})")
        print(f"  Adoptés: {adopte:,} ({pct(adopte, total_amend)})")

        # 7. Encoding
        bad_enc_int = q("SELECT count(*) FROM interventions WHERE contenu LIKE '%Ã©%' OR contenu LIKE '%Ã¨%'")
        bad_enc_am = q("SELECT count(*) FROM amendements WHERE contenu LIKE '%Ã©%' OR expose_motifs LIKE '%Ã©%'")
        print(f"\n--- ENCODING ---")
        print(f"  Interventions: {bad_enc_int:,}")
        print(f"  Amendements: {bad_enc_am:,}")

        # 8. Verdict embedding
        deb_with_ci = q("SELECT count(*) FROM debats WHERE contenu_integral IS NOT NULL AND contenu_integral != ''")
        print(f"\n--- VERDICT EMBEDDING ---")
        print(f"  Débats avec contenu_integral: {deb_with_ci:,}/{total_deb:,} ({pct(deb_with_ci, total_deb)})")
        print(f"  Interventions exploitables: {usable_int:,}/{total_int:,} ({pct(usable_int, total_int)})")
        print(f"  Dossiers complets: {total_dos}/{total_lois * 2}")
        print(f"  Orphelins: {orphan_deb}")
        print(f"  Encoding OK: {'OUI' if bad_enc_int + bad_enc_am == 0 else 'NON'}")


if __name__ == "__main__":
    main()
