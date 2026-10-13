#!/usr/bin/env python3
"""Analyse comparative de la qualité des données : pre-2002 vs 2002+."""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.disable(logging.INFO)

from sqlalchemy import text

from src.db.models import get_engine

CUTOFF_YEAR = 2002
CUTOFF_DATE = "2002-01-01"
BAR_WIDTH = 40


def pct(n, total):
    return f"{100 * n / max(total, 1):.1f}%"


def fmt(n):
    if n is None:
        return "—"
    if isinstance(n, float):
        return f"{n:,.1f}"
    return f"{n:,}"


def bar(n, max_val):
    length = int(n / max(max_val, 1) * BAR_WIDTH)
    return "█" * length


def section(title, num):
    print(f"\n{'=' * 80}")
    print(f"  {num}. {title}")
    print(f"{'=' * 80}")


def row(label, pre, post, w=30):
    print(f"  {label:<{w}} {str(pre):>12}   {str(post):>12}")


def main():
    engine = get_engine()
    t0 = time.time()

    with engine.connect() as c:
        def q(sql, params=None):
            return c.execute(text(sql), params or {}).scalar()

        def qall(sql, params=None):
            return c.execute(text(sql), params or {}).fetchall()

        print("=" * 80)
        print("  ANALYSE QUALITÉ DES DONNÉES — pre-2002 vs 2002+")
        print("=" * 80)
        print(f"  Base : {engine.url}")
        print(f"  Coupure : {CUTOFF_DATE}")

        # =================================================================
        # 1. VOLUME
        # =================================================================
        section("VOLUME", 1)

        row("Table", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")

        tables_volume = qall(f"""
            SELECT 'lois' AS tbl,
                COUNT(*) FILTER (WHERE annee < {CUTOFF_YEAR}) AS pre,
                COUNT(*) FILTER (WHERE annee >= {CUTOFF_YEAR}) AS post
            FROM lois
            UNION ALL
            SELECT 'articles',
                COUNT(*) FILTER (WHERE l.annee < {CUTOFF_YEAR}),
                COUNT(*) FILTER (WHERE l.annee >= {CUTOFF_YEAR})
            FROM articles a JOIN lois l ON l.id = a.loi_id
            UNION ALL
            SELECT 'dossiers',
                COUNT(*) FILTER (WHERE l.annee < {CUTOFF_YEAR}),
                COUNT(*) FILTER (WHERE l.annee >= {CUTOFF_YEAR})
            FROM dossiers_legislatifs d JOIN lois l ON l.id = d.loi_id
            UNION ALL
            SELECT 'debats',
                COUNT(*) FILTER (WHERE date < '{CUTOFF_DATE}'),
                COUNT(*) FILTER (WHERE date >= '{CUTOFF_DATE}')
            FROM debats
            UNION ALL
            SELECT 'interventions',
                COUNT(*) FILTER (WHERE db.date < '{CUTOFF_DATE}'),
                COUNT(*) FILTER (WHERE db.date >= '{CUTOFF_DATE}')
            FROM interventions i JOIN debats db ON db.id = i.debat_id
            UNION ALL
            SELECT 'amendements',
                COUNT(*) FILTER (WHERE COALESCE(a.date_depot, make_date(l.annee, 1, 1)) < '{CUTOFF_DATE}'),
                COUNT(*) FILTER (WHERE COALESCE(a.date_depot, make_date(l.annee, 1, 1)) >= '{CUTOFF_DATE}')
            FROM amendements a
            JOIN dossiers_legislatifs d ON d.id = a.dossier_id
            JOIN lois l ON l.id = d.loi_id
            UNION ALL
            SELECT 'sources_externes',
                COUNT(*) FILTER (WHERE date < '{CUTOFF_DATE}'),
                COUNT(*) FILTER (WHERE date >= '{CUTOFF_DATE}')
            FROM sources_externes
        """)
        for tbl, pre, post in tables_volume:
            row(tbl, fmt(pre), fmt(post))
        print()

        # Répartition AN/Sénat
        print("  Répartition par chambre :")
        chamber_split = qall(f"""
            SELECT chambre,
                COUNT(*) FILTER (WHERE date < '{CUTOFF_DATE}') AS pre,
                COUNT(*) FILTER (WHERE date >= '{CUTOFF_DATE}') AS post
            FROM debats
            GROUP BY chambre ORDER BY chambre
        """)
        row("  Débats", "Pre-2002", "2002+")
        for ch, pre, post in chamber_split:
            row(f"    {ch}", fmt(pre), fmt(post))

        chamber_dos = qall(f"""
            SELECT d.chambre,
                COUNT(*) FILTER (WHERE l.annee < {CUTOFF_YEAR}) AS pre,
                COUNT(*) FILTER (WHERE l.annee >= {CUTOFF_YEAR}) AS post
            FROM dossiers_legislatifs d JOIN lois l ON l.id = d.loi_id
            GROUP BY d.chambre ORDER BY d.chambre
        """)
        print()
        row("  Dossiers", "Pre-2002", "2002+")
        for ch, pre, post in chamber_dos:
            row(f"    {ch}", fmt(pre), fmt(post))

        # Histogramme par tranche de 5 ans (débats)
        print("\n  Débats par tranche de 5 ans :")
        buckets = qall("""
            SELECT (EXTRACT(YEAR FROM date)::int / 5) * 5 AS bucket,
                COUNT(*) AS cnt
            FROM debats
            GROUP BY bucket ORDER BY bucket
        """)
        if buckets:
            max_cnt = max(r[1] for r in buckets)
            for bucket, cnt in buckets:
                label = f"  {int(bucket)}-{int(bucket)+4}"
                print(f"    {label}  {bar(cnt, max_cnt)} {cnt:,}")

        # =================================================================
        # 2. COMPLÉTUDE
        # =================================================================
        section("COMPLÉTUDE (% NULL ou vide)", 2)

        # -- Lois --
        print("\n  LOIS :")
        row("Champ", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")
        lois_comp = qall(f"""
            SELECT
                CASE WHEN annee < {CUTOFF_YEAR} THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE titre IS NULL OR titre = '') AS titre_null,
                COUNT(*) FILTER (WHERE nor IS NULL OR nor = '') AS nor_null,
                COUNT(*) FILTER (WHERE url_legifrance IS NULL OR url_legifrance = '') AS url_null,
                COUNT(*) FILTER (WHERE date_promulgation IS NULL) AS date_null
            FROM lois GROUP BY period ORDER BY period
        """)
        lois_map = {r[0]: r for r in lois_comp}
        pre = lois_map.get("pre", ("pre", 0, 0, 0, 0, 0))
        post = lois_map.get("post", ("post", 0, 0, 0, 0, 0))
        for i, col in enumerate(["titre", "nor", "url_legifrance", "date_promulgation"], 2):
            row(col, pct(pre[i], pre[1]), pct(post[i], post[1]))

        # -- Articles --
        print("\n  ARTICLES :")
        row("Champ", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")
        art_comp = qall(f"""
            SELECT
                CASE WHEN l.annee < {CUTOFF_YEAR} THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE a.contenu_texte IS NULL OR a.contenu_texte = '') AS contenu_null
            FROM articles a JOIN lois l ON l.id = a.loi_id
            GROUP BY period ORDER BY period
        """)
        art_map = {r[0]: r for r in art_comp}
        pre = art_map.get("pre", ("pre", 0, 0))
        post = art_map.get("post", ("post", 0, 0))
        row("contenu_texte", pct(pre[2], pre[1]), pct(post[2], post[1]))

        # -- Débats --
        print("\n  DÉBATS :")
        row("Champ", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")
        deb_comp = qall(f"""
            SELECT
                CASE WHEN date < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE contenu_integral IS NULL OR contenu_integral = '') AS ci_null,
                COUNT(*) FILTER (WHERE url_source IS NULL OR url_source = '') AS url_null,
                COUNT(*) FILTER (WHERE etape IS NULL OR etape = '') AS etape_null
            FROM debats GROUP BY period ORDER BY period
        """)
        deb_map = {r[0]: r for r in deb_comp}
        pre = deb_map.get("pre", ("pre", 0, 0, 0, 0))
        post = deb_map.get("post", ("post", 0, 0, 0, 0))
        for i, col in enumerate(["contenu_integral", "url_source", "etape"], 2):
            row(col, pct(pre[i], pre[1]), pct(post[i], post[1]))

        # -- Interventions --
        print("\n  INTERVENTIONS :")
        row("Champ", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")
        int_comp = qall(f"""
            SELECT
                CASE WHEN db.date < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE i.orateur IS NULL OR i.orateur = '') AS orateur_null,
                COUNT(*) FILTER (WHERE i.fonction IS NULL) AS fonction_null,
                COUNT(*) FILTER (WHERE i.contenu IS NULL OR i.contenu = '') AS contenu_null,
                COUNT(*) FILTER (WHERE i.articles_mentionnes IS NULL) AS am_null
            FROM interventions i JOIN debats db ON db.id = i.debat_id
            GROUP BY period ORDER BY period
        """)
        int_map = {r[0]: r for r in int_comp}
        pre = int_map.get("pre", ("pre", 0, 0, 0, 0, 0))
        post = int_map.get("post", ("post", 0, 0, 0, 0, 0))
        for i, col in enumerate(["orateur", "fonction", "contenu", "articles_mentionnes"], 2):
            row(col, pct(pre[i], pre[1]), pct(post[i], post[1]))

        # -- Amendements --
        print("\n  AMENDEMENTS :")
        row("Champ", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")
        am_comp = qall(f"""
            SELECT
                CASE WHEN COALESCE(a.date_depot, make_date(l.annee, 1, 1)) < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE a.contenu IS NULL OR a.contenu = '') AS contenu_null,
                COUNT(*) FILTER (WHERE a.expose_motifs IS NULL OR a.expose_motifs = '') AS expose_null,
                COUNT(*) FILTER (WHERE a.sort IS NULL OR a.sort = '') AS sort_null,
                COUNT(*) FILTER (WHERE a.date_depot IS NULL) AS date_null,
                COUNT(*) FILTER (WHERE a.auteur IS NULL OR a.auteur = '') AS auteur_null
            FROM amendements a
            JOIN dossiers_legislatifs d ON d.id = a.dossier_id
            JOIN lois l ON l.id = d.loi_id
            GROUP BY period ORDER BY period
        """)
        am_map = {r[0]: r for r in am_comp}
        pre = am_map.get("pre", ("pre", 0, 0, 0, 0, 0, 0))
        post = am_map.get("post", ("post", 0, 0, 0, 0, 0, 0))
        for i, col in enumerate(["contenu", "expose_motifs", "sort", "date_depot", "auteur"], 2):
            row(col, pct(pre[i], pre[1]), pct(post[i], post[1]))

        # =================================================================
        # 3. RICHESSE DU CONTENU
        # =================================================================
        section("RICHESSE DU CONTENU (longueur texte)", 3)

        # Interventions
        print("\n  interventions.contenu :")
        row("Stat", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")
        rich_int = qall(f"""
            SELECT
                CASE WHEN db.date < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS n,
                ROUND(AVG(LENGTH(i.contenu))) AS avg_len,
                ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY LENGTH(i.contenu))::numeric) AS median_len,
                MIN(LENGTH(i.contenu)) AS min_len,
                MAX(LENGTH(i.contenu)) AS max_len
            FROM interventions i JOIN debats db ON db.id = i.debat_id
            WHERE i.contenu IS NOT NULL AND i.contenu != ''
            GROUP BY period ORDER BY period
        """)
        rich_int_map = {r[0]: r for r in rich_int}
        pre = rich_int_map.get("pre", ("pre", 0, 0, 0, 0, 0))
        post = rich_int_map.get("post", ("post", 0, 0, 0, 0, 0))
        for i, label in enumerate(["n (non-vides)", "moyenne", "médiane", "min", "max"], 1):
            row(label, fmt(pre[i]), fmt(post[i]))

        # Débats contenu_integral
        print("\n  debats.contenu_integral :")
        row("Stat", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")
        rich_deb = qall(f"""
            SELECT
                CASE WHEN date < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS n,
                ROUND(AVG(LENGTH(contenu_integral))) AS avg_len,
                ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY LENGTH(contenu_integral))::numeric) AS median_len,
                MIN(LENGTH(contenu_integral)) AS min_len,
                MAX(LENGTH(contenu_integral)) AS max_len
            FROM debats
            WHERE contenu_integral IS NOT NULL AND contenu_integral != ''
            GROUP BY period ORDER BY period
        """)
        rich_deb_map = {r[0]: r for r in rich_deb}
        pre = rich_deb_map.get("pre", ("pre", 0, 0, 0, 0, 0))
        post = rich_deb_map.get("post", ("post", 0, 0, 0, 0, 0))
        for i, label in enumerate(["n (non-vides)", "moyenne", "médiane", "min", "max"], 1):
            row(label, fmt(pre[i]), fmt(post[i]))

        # Amendements contenu + expose_motifs
        print("\n  amendements.contenu :")
        row("Stat", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")
        rich_am = qall(f"""
            SELECT
                CASE WHEN COALESCE(a.date_depot, make_date(l.annee, 1, 1)) < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS n,
                ROUND(AVG(LENGTH(a.contenu))) AS avg_len,
                ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY LENGTH(a.contenu))::numeric) AS median_len
            FROM amendements a
            JOIN dossiers_legislatifs d ON d.id = a.dossier_id
            JOIN lois l ON l.id = d.loi_id
            WHERE a.contenu IS NOT NULL AND a.contenu != ''
            GROUP BY period ORDER BY period
        """)
        rich_am_map = {r[0]: r for r in rich_am}
        pre = rich_am_map.get("pre", ("pre", 0, 0, 0))
        post = rich_am_map.get("post", ("post", 0, 0, 0))
        for i, label in enumerate(["n (non-vides)", "moyenne", "médiane"], 1):
            row(label, fmt(pre[i]), fmt(post[i]))

        print("\n  amendements.expose_motifs :")
        row("Stat", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")
        rich_exp = qall(f"""
            SELECT
                CASE WHEN COALESCE(a.date_depot, make_date(l.annee, 1, 1)) < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS n,
                ROUND(AVG(LENGTH(a.expose_motifs))) AS avg_len,
                ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY LENGTH(a.expose_motifs))::numeric) AS median_len
            FROM amendements a
            JOIN dossiers_legislatifs d ON d.id = a.dossier_id
            JOIN lois l ON l.id = d.loi_id
            WHERE a.expose_motifs IS NOT NULL AND a.expose_motifs != ''
            GROUP BY period ORDER BY period
        """)
        rich_exp_map = {r[0]: r for r in rich_exp}
        pre = rich_exp_map.get("pre", ("pre", 0, 0, 0))
        post = rich_exp_map.get("post", ("post", 0, 0, 0))
        for i, label in enumerate(["n (non-vides)", "moyenne", "médiane"], 1):
            row(label, fmt(pre[i]), fmt(post[i]))

        # Articles contenu_texte
        print("\n  articles.contenu_texte :")
        row("Stat", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")
        rich_art = qall(f"""
            SELECT
                CASE WHEN l.annee < {CUTOFF_YEAR} THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS n,
                ROUND(AVG(LENGTH(a.contenu_texte))) AS avg_len,
                ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY LENGTH(a.contenu_texte))::numeric) AS median_len
            FROM articles a JOIN lois l ON l.id = a.loi_id
            WHERE a.contenu_texte IS NOT NULL AND a.contenu_texte != ''
            GROUP BY period ORDER BY period
        """)
        rich_art_map = {r[0]: r for r in rich_art}
        pre = rich_art_map.get("pre", ("pre", 0, 0, 0))
        post = rich_art_map.get("post", ("post", 0, 0, 0))
        for i, label in enumerate(["n (non-vides)", "moyenne", "médiane"], 1):
            row(label, fmt(pre[i]), fmt(post[i]))

        # =================================================================
        # 4. QUALITÉ DU CHAÎNAGE
        # =================================================================
        section("QUALITÉ DU CHAÎNAGE", 4)

        row("Métrique", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")

        # Débats orphelins
        orphan = qall(f"""
            SELECT
                CASE WHEN date < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE dossier_id IS NULL) AS orphaned
            FROM debats GROUP BY period ORDER BY period
        """)
        orphan_map = {r[0]: r for r in orphan}
        pre = orphan_map.get("pre", ("pre", 0, 0))
        post = orphan_map.get("post", ("post", 0, 0))
        row("Débats orphelins",
            f"{pre[2]:,} / {pre[1]:,} ({pct(pre[2], pre[1])})",
            f"{post[2]:,} / {post[1]:,} ({pct(post[2], post[1])})")

        # Amendements sans article
        noart = qall(f"""
            SELECT
                CASE WHEN COALESCE(a.date_depot, make_date(l.annee, 1, 1)) < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE a.article_id IS NULL) AS no_art
            FROM amendements a
            JOIN dossiers_legislatifs d ON d.id = a.dossier_id
            JOIN lois l ON l.id = d.loi_id
            GROUP BY period ORDER BY period
        """)
        noart_map = {r[0]: r for r in noart}
        pre = noart_map.get("pre", ("pre", 0, 0))
        post = noart_map.get("post", ("post", 0, 0))
        row("Amend. sans article",
            f"{pre[2]:,} / {pre[1]:,} ({pct(pre[2], pre[1])})",
            f"{post[2]:,} / {post[1]:,} ({pct(post[2], post[1])})")

        # Débats sans intervention
        no_int = qall(f"""
            SELECT
                CASE WHEN d.date < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE NOT EXISTS (
                    SELECT 1 FROM interventions i WHERE i.debat_id = d.id
                )) AS empty
            FROM debats d GROUP BY period ORDER BY period
        """)
        no_int_map = {r[0]: r for r in no_int}
        pre = no_int_map.get("pre", ("pre", 0, 0))
        post = no_int_map.get("post", ("post", 0, 0))
        row("Débats sans interv.",
            f"{pre[2]:,} / {pre[1]:,} ({pct(pre[2], pre[1])})",
            f"{post[2]:,} / {post[1]:,} ({pct(post[2], post[1])})")

        # Lois sans articles
        lois_noart = qall(f"""
            SELECT
                CASE WHEN l.annee < {CUTOFF_YEAR} THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE NOT EXISTS (
                    SELECT 1 FROM articles a WHERE a.loi_id = l.id
                )) AS no_art
            FROM lois l GROUP BY period ORDER BY period
        """)
        lois_noart_map = {r[0]: r for r in lois_noart}
        pre = lois_noart_map.get("pre", ("pre", 0, 0))
        post = lois_noart_map.get("post", ("post", 0, 0))
        row("Lois sans articles",
            f"{pre[2]:,} / {pre[1]:,} ({pct(pre[2], pre[1])})",
            f"{post[2]:,} / {post[1]:,} ({pct(post[2], post[1])})")

        # Lois sans dossier AN / Sénat
        lois_nodos = qall(f"""
            SELECT
                CASE WHEN l.annee < {CUTOFF_YEAR} THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE NOT EXISTS (
                    SELECT 1 FROM dossiers_legislatifs d WHERE d.loi_id = l.id AND d.chambre = 'AN'
                )) AS no_an,
                COUNT(*) FILTER (WHERE NOT EXISTS (
                    SELECT 1 FROM dossiers_legislatifs d WHERE d.loi_id = l.id AND d.chambre = 'SENAT'
                )) AS no_senat
            FROM lois l GROUP BY period ORDER BY period
        """)
        lois_nodos_map = {r[0]: r for r in lois_nodos}
        pre = lois_nodos_map.get("pre", ("pre", 0, 0, 0))
        post = lois_nodos_map.get("post", ("post", 0, 0, 0))
        row("Lois sans dossier AN",
            f"{pre[2]:,} / {pre[1]:,} ({pct(pre[2], pre[1])})",
            f"{post[2]:,} / {post[1]:,} ({pct(post[2], post[1])})")
        row("Lois sans dossier Sénat",
            f"{pre[3]:,} / {pre[1]:,} ({pct(pre[3], pre[1])})",
            f"{post[3]:,} / {post[1]:,} ({pct(post[3], post[1])})")

        # =================================================================
        # 5. DISTRIBUTION TEMPORELLE
        # =================================================================
        section("DISTRIBUTION TEMPORELLE", 5)

        # Débats par année
        print("\n  Débats par année :")
        deb_years = qall("""
            SELECT EXTRACT(YEAR FROM date)::int AS yr, COUNT(*) AS cnt
            FROM debats GROUP BY yr ORDER BY yr
        """)
        if deb_years:
            max_cnt = max(r[1] for r in deb_years)
            for yr, cnt in deb_years:
                marker = " «" if yr == CUTOFF_YEAR else ""
                print(f"    {yr}  {bar(cnt, max_cnt)} {cnt:,}{marker}")

        # Interventions par année
        print("\n  Interventions par année :")
        int_years = qall("""
            SELECT EXTRACT(YEAR FROM db.date)::int AS yr, COUNT(*) AS cnt
            FROM interventions i JOIN debats db ON db.id = i.debat_id
            GROUP BY yr ORDER BY yr
        """)
        if int_years:
            max_cnt = max(r[1] for r in int_years)
            for yr, cnt in int_years:
                marker = " «" if yr == CUTOFF_YEAR else ""
                print(f"    {yr}  {bar(cnt, max_cnt)} {cnt:,}{marker}")

        # =================================================================
        # 6. ANOMALIES
        # =================================================================
        section("ANOMALIES", 6)

        # Dates suspectes
        print("\n  Dates suspectes (débats) :")
        bad_dates = qall("""
            SELECT id, date, chambre, reference_externe
            FROM debats
            WHERE EXTRACT(YEAR FROM date) < 1958 OR EXTRACT(YEAR FROM date) > 2026
            ORDER BY date
            LIMIT 20
        """)
        if bad_dates:
            for did, ddate, ch, ref in bad_dates:
                print(f"    id={did} date={ddate} chambre={ch} ref={ref}")
        else:
            print("    Aucune date suspecte")

        # Encoding
        print("\n  Problèmes d'encoding :")
        row("Table", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")
        enc_int = qall(f"""
            SELECT
                CASE WHEN db.date < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) FILTER (WHERE i.contenu LIKE '%Ã©%' OR i.contenu LIKE '%Ã¨%'
                    OR i.contenu LIKE '%Ã%') AS bad
            FROM interventions i JOIN debats db ON db.id = i.debat_id
            GROUP BY period ORDER BY period
        """)
        enc_int_map = {r[0]: r for r in enc_int}
        row("interventions",
            fmt(enc_int_map.get("pre", ("pre", 0))[1]),
            fmt(enc_int_map.get("post", ("post", 0))[1]))

        enc_am = qall(f"""
            SELECT
                CASE WHEN COALESCE(a.date_depot, make_date(l.annee, 1, 1)) < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) FILTER (WHERE a.contenu LIKE '%Ã©%' OR a.expose_motifs LIKE '%Ã©%'
                    OR a.contenu LIKE '%Ã%') AS bad
            FROM amendements a
            JOIN dossiers_legislatifs d ON d.id = a.dossier_id
            JOIN lois l ON l.id = d.loi_id
            GROUP BY period ORDER BY period
        """)
        enc_am_map = {r[0]: r for r in enc_am}
        row("amendements",
            fmt(enc_am_map.get("pre", ("pre", 0))[1]),
            fmt(enc_am_map.get("post", ("post", 0))[1]))

        # Interventions courtes (< 50 chars)
        print("\n  Interventions courtes (< 50 chars) :")
        short_int = qall(f"""
            SELECT
                CASE WHEN db.date < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE LENGTH(i.contenu) < 50) AS short
            FROM interventions i JOIN debats db ON db.id = i.debat_id
            WHERE i.contenu IS NOT NULL
            GROUP BY period ORDER BY period
        """)
        short_map = {r[0]: r for r in short_int}
        pre = short_map.get("pre", ("pre", 0, 0))
        post = short_map.get("post", ("post", 0, 0))
        row("< 50 chars",
            f"{pre[2]:,} / {pre[1]:,} ({pct(pre[2], pre[1])})",
            f"{post[2]:,} / {post[1]:,} ({pct(post[2], post[1])})")

        # =================================================================
        # 7. ANALYSE CROISÉE
        # =================================================================
        section("ANALYSE CROISÉE", 7)

        row("Métrique", "Pre-2002", "2002+")
        print(f"  {'─' * 30} {'─' * 12}   {'─' * 12}")

        # Moy. débats par loi
        avg_deb = qall(f"""
            SELECT period, ROUND(AVG(cnt), 1) AS avg_cnt FROM (
                SELECT
                    CASE WHEN l.annee < {CUTOFF_YEAR} THEN 'pre' ELSE 'post' END AS period,
                    (SELECT COUNT(*) FROM debats db
                     JOIN dossiers_legislatifs dl ON db.dossier_id = dl.id
                     WHERE dl.loi_id = l.id) AS cnt
                FROM lois l
            ) sub GROUP BY period ORDER BY period
        """)
        avg_deb_map = {r[0]: r[1] for r in avg_deb}
        row("Moy. débats / loi",
            fmt(avg_deb_map.get("pre", 0)),
            fmt(avg_deb_map.get("post", 0)))

        # Moy. interventions par débat
        avg_int = qall(f"""
            SELECT period, ROUND(AVG(cnt), 1) AS avg_cnt FROM (
                SELECT
                    CASE WHEN d.date < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                    (SELECT COUNT(*) FROM interventions i WHERE i.debat_id = d.id) AS cnt
                FROM debats d
            ) sub GROUP BY period ORDER BY period
        """)
        avg_int_map = {r[0]: r[1] for r in avg_int}
        row("Moy. interv. / débat",
            fmt(avg_int_map.get("pre", 0)),
            fmt(avg_int_map.get("post", 0)))

        # Moy. amendements par loi
        avg_am = qall(f"""
            SELECT period, ROUND(AVG(cnt), 1) AS avg_cnt FROM (
                SELECT
                    CASE WHEN l.annee < {CUTOFF_YEAR} THEN 'pre' ELSE 'post' END AS period,
                    (SELECT COUNT(*) FROM amendements am
                     JOIN dossiers_legislatifs dl ON am.dossier_id = dl.id
                     WHERE dl.loi_id = l.id) AS cnt
                FROM lois l
            ) sub GROUP BY period ORDER BY period
        """)
        avg_am_map = {r[0]: r[1] for r in avg_am}
        row("Moy. amend. / loi",
            fmt(avg_am_map.get("pre", 0)),
            fmt(avg_am_map.get("post", 0)))

        # Couverture
        print()
        coverage = qall(f"""
            SELECT
                CASE WHEN l.annee < {CUTOFF_YEAR} THEN 'pre' ELSE 'post' END AS period,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM dossiers_legislatifs dl
                    JOIN debats db ON db.dossier_id = dl.id
                    WHERE dl.loi_id = l.id
                )) AS with_debate,
                COUNT(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM dossiers_legislatifs dl
                    JOIN amendements am ON am.dossier_id = dl.id
                    WHERE dl.loi_id = l.id
                )) AS with_amend
            FROM lois l GROUP BY period ORDER BY period
        """)
        cov_map = {r[0]: r for r in coverage}
        pre = cov_map.get("pre", ("pre", 0, 0, 0))
        post = cov_map.get("post", ("post", 0, 0, 0))
        row("Lois avec ≥1 débat",
            f"{pre[2]:,} / {pre[1]:,} ({pct(pre[2], pre[1])})",
            f"{post[2]:,} / {post[1]:,} ({pct(post[2], post[1])})")
        row("Lois avec ≥1 amend.",
            f"{pre[3]:,} / {pre[1]:,} ({pct(pre[3], pre[1])})",
            f"{post[3]:,} / {post[1]:,} ({pct(post[3], post[1])})")

        # Distribution des fonctions
        print("\n  Distribution des rôles (fonction) :")
        print(f"  {'─' * 56}")
        fonctions = qall(f"""
            SELECT
                CASE WHEN db.date < '{CUTOFF_DATE}' THEN 'pre' ELSE 'post' END AS period,
                COALESCE(i.fonction, '(NULL)') AS fn,
                COUNT(*) AS cnt
            FROM interventions i JOIN debats db ON db.id = i.debat_id
            GROUP BY period, fn ORDER BY period, cnt DESC
        """)
        pre_fn = [(fn, cnt) for p, fn, cnt in fonctions if p == "pre"]
        post_fn = [(fn, cnt) for p, fn, cnt in fonctions if p == "post"]
        pre_total = sum(c for _, c in pre_fn) or 1
        post_total = sum(c for _, c in post_fn) or 1

        print(f"  {'Pre-2002':>40}    {'2002+':>40}")
        max_rows = max(len(pre_fn), len(post_fn))
        for i in range(min(max_rows, 10)):
            left = f"{pre_fn[i][0]}: {pre_fn[i][1]:,} ({pct(pre_fn[i][1], pre_total)})" if i < len(pre_fn) else ""
            right = f"{post_fn[i][0]}: {post_fn[i][1]:,} ({pct(post_fn[i][1], post_total)})" if i < len(post_fn) else ""
            print(f"    {left:<38}  {right}")

        # =================================================================
        # FIN
        # =================================================================
        elapsed = time.time() - t0
        print(f"\n{'=' * 80}")
        print(f"  Analyse terminée en {elapsed:.1f}s")
        print(f"{'=' * 80}")


if __name__ == "__main__":
    main()
