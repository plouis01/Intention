#!/usr/bin/env python3
"""
Patch empty intentions in the analysis_cache for article 39.
Intentions are written by hand based on the legislative text, context, and sources.

Usage:
    python scripts/patch_empty_intentions.py
    python scripts/patch_empty_intentions.py --dry-run
"""

import argparse
import json
import sys
from datetime import datetime, UTC
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.db.models import AnalysisCache, get_session

# Hand-written intentions for each empty section, keyed by section index
INTENTIONS = {
    1: (  # LF 2004 (n°2003-1311) / Art. 89
        "L'intention du législateur est d'assurer la cohérence du code général des impôts "
        "en supprimant des dispositifs de coordination devenus obsolètes.\n\n"
        "Le législateur souhaite :\n"
        "- Supprimer les références croisées devenues sans objet à la suite de réformes antérieures\n"
        "- Garantir la lisibilité et la cohérence interne du CGI en nettoyant les dispositions caduques"
    ),
    2: (  # LF 2005 (n°2004-1484) / Art. 36 — PPHP
        "L'intention du législateur est de plafonner la provision pour hausse des prix (PPHP) "
        "prévue à l'article 39-1-5° du CGI afin de limiter l'avantage fiscal qu'elle procure.\n\n"
        "Le législateur souhaite :\n"
        "- Limiter le montant de la dotation de la provision pour hausse des prix à 15 millions d'euros "
        "par période de douze mois\n"
        "- Encadrer un dispositif de déduction jugé excessivement favorable aux grandes entreprises\n"
        "- Réduire le coût budgétaire de cette dépense fiscale"
    ),
    3: (  # LF 2006 (n°2005-1719) / Art. 17, 25 — provisions pour dépréciation
        "L'intention du législateur est d'encadrer le régime des provisions pour dépréciation "
        "des titres de participation et des immeubles de placement, dans un souci de transparence "
        "et d'équilibre fiscal.\n\n"
        "Le législateur souhaite :\n"
        "- Obtenir plus de transparence dans le traitement fiscal des provisions pour dépréciation\n"
        "- Mettre en place une réforme équilibrée du régime global prévu à l'article 39\n"
        "- Harmoniser les règles de déductibilité des provisions avec les normes comptables"
    ),
    4: (  # LF 2007 (n°2006-1666) / Art. 22
        "L'intention du législateur est de modifier les règles de déductibilité des charges "
        "dans le cadre d'un ensemble de réformes fiscales portant sur l'imposition des résultats "
        "des entreprises.\n\n"
        "Le législateur souhaite :\n"
        "- Adapter le régime de déduction des charges au contexte économique et aux évolutions "
        "de la fiscalité des entreprises\n"
        "- Clarifier les conditions de déductibilité de certaines charges spécifiques"
    ),
    9: (  # LFR 2008 (n°2008-1443) / Art. 23
        "L'intention du législateur est d'adapter les règles de déductibilité des charges dans le "
        "contexte de la crise financière de 2008, en ajustant les dispositions relatives aux charges "
        "déductibles du bénéfice imposable.\n\n"
        "Le législateur souhaite :\n"
        "- Répondre aux difficultés économiques nées de la crise financière par des mesures d'ajustement fiscal\n"
        "- Adapter le traitement des charges déductibles aux conditions exceptionnelles du marché"
    ),
    10: (  # LF 2011 (n°2010-1657) / Art. 126, 24
        "L'intention du législateur est de coordonner les dispositions de l'article 39 du CGI "
        "avec les réformes du financement de la sécurité sociale, en ajustant le traitement des "
        "cotisations et des transferts de TVA entre l'État et les organismes de protection sociale.\n\n"
        "Le législateur souhaite :\n"
        "- Clarifier le traitement fiscal des cotisations accidents du travail dans les charges déductibles\n"
        "- Coordonner la modification avec le transfert de TVA à l'assurance maladie (article 12 de la loi)\n"
        "- Assurer la cohérence entre la fiscalité des entreprises et le financement de la protection sociale"
    ),
    11: (  # LF 2012 (n°2011-1977) / Art. 11
        "L'intention du législateur est de renforcer l'encadrement des liens de dépendance "
        "entre entreprises pour la déduction des charges (article 39-1-12° du CGI) et de "
        "supprimer des régimes dérogatoires obsolètes.\n\n"
        "Le législateur souhaite :\n"
        "- Réécrire le premier alinéa du 12 de l'article 39 pour clarifier la définition des "
        "liens de dépendance entre entreprise concédante et entreprise concessionnaire\n"
        "- Abroger l'article 39 ter du CGI (provision pour reconstitution des gisements "
        "d'hydrocarbures) à compter du 1er janvier 2014, jugé obsolète\n"
        "- Simplifier le code en supprimant des dispositions dérogatoires devenues sans objet"
    ),
    12: (  # LFR 2011 (n°2011-900) / Art. 16
        "L'intention du législateur est de procéder à des ajustements techniques de l'article 39 "
        "du CGI dans le cadre de la loi de finances rectificative de juillet 2011.\n\n"
        "Le législateur souhaite :\n"
        "- Adapter les dispositions relatives aux charges déductibles aux évolutions législatives "
        "intervenues depuis la dernière modification\n"
        "- Assurer la cohérence du dispositif avec les autres dispositions fiscales de la LFR"
    ),
    15: (  # LF 2014 (n°2013-1278) / Art. 26, 15
        "L'intention du législateur est de modifier les règles de déductibilité des charges "
        "liées aux amortissements des logiciels et au plafonnement des rémunérations, "
        "tout en supprimant des dispositions obsolètes.\n\n"
        "Le législateur souhaite :\n"
        "- Modifier le 5° du 1 de l'article 39 en supprimant certains alinéas relatifs aux "
        "provisions pour dépréciation\n"
        "- Encadrer les conditions de déductibilité des rémunérations au regard du résultat "
        "imposable (article 39-1-1°)\n"
        "- Clarifier le régime fiscal des brevets et logiciels acquis en conformité avec le "
        "5 bis de l'article 39"
    ),
    16: (  # LF 2015 (n°2014-1654) / Art. 26, 70
        "L'intention du législateur est de raccourcir la durée d'amortissement des brevets "
        "et d'abroger des dispositions obsolètes du CGI.\n\n"
        "Le législateur souhaite :\n"
        "- Réduire de six à trois ans la durée d'amortissement des brevets prévue au 5 bis "
        "de l'article 39, pour accélérer la déduction fiscale et stimuler l'innovation\n"
        "- Abroger le 7 de l'article 39 et le 4° du 1 de l'article 93, devenus sans objet, "
        "dans un souci de simplification du code"
    ),
    17: (  # LFR 2014 (n°2014-1655) / Art. 79, 26
        "L'intention du législateur est de renforcer la lutte contre l'évasion fiscale et "
        "l'optimisation agressive en matière de prix de transfert, et d'élargir l'assiette "
        "des taxes assises sur les charges déductibles.\n\n"
        "Le législateur souhaite :\n"
        "- Qualifier en revenus distribués les bénéfices transférés (article 57 du CGI) "
        "et les produits relevant de l'article 238 A, pour en limiter la déductibilité\n"
        "- Ajouter les références aux articles 231 ter, 235 ter X, 235 ter ZE et 235 ter ZE bis "
        "au 4° du 1 de l'article 39, élargissant la liste des taxes déductibles du bénéfice imposable"
    ),
    20: (  # LF 2017 (n°2016-1917) / Art. 70, 109
        "L'intention du législateur est de modifier le régime des pénalités de retard déductibles "
        "et d'autoriser l'administration fiscale à recourir à des aviseurs fiscaux rémunérés.\n\n"
        "Le législateur souhaite :\n"
        "- Supprimer la seconde phrase du a du 4 de l'article 39 relative aux pénalités de retard, "
        "en simplifiant le régime de déductibilité des intérêts moratoires\n"
        "- Créer un dispositif expérimental d'indemnisation des aviseurs fiscaux (personnes "
        "signalant des fraudes), dans le cadre de la lutte contre la fraude fiscale"
    ),
    21: (  # LFR 2016 (n°2016-1918) / Art. 70, 56, 91
        "L'intention du législateur est d'ajouter de nouvelles taxes déductibles du bénéfice "
        "imposable et de renforcer les conditions de déductibilité des provisions pour dépréciation "
        "des titres de participation.\n\n"
        "Le législateur souhaite :\n"
        "- Insérer un 6° bis au 1 de l'article 39 pour rendre déductible la taxe sur la diffusion "
        "en vidéo physique et en ligne de contenus audiovisuels (article 1609 sexdecies B du CGI)\n"
        "- Conditionner la déductibilité des provisions pour dépréciation de titres de participation "
        "à la détention d'au moins 5 % des droits de vote de la société émettrice\n"
        "- Adapter les dispositions relatives aux réductions d'impôt outre-mer"
    ),
    23: (  # LFSS 2018 (n°2017-1836) / Art. 10, 4
        "L'intention du législateur est de coordonner les dispositions de l'article 39 du CGI "
        "avec la réforme du financement de la sécurité sociale opérée par la LFSS 2018, "
        "notamment la suppression de certaines cotisations salariales compensée par une hausse "
        "de la CSG.\n\n"
        "Le législateur souhaite :\n"
        "- Adapter le traitement des charges sociales déductibles à la suppression des cotisations "
        "salariales maladie et chômage\n"
        "- Abroger les dispositions devenues sans objet du fait de la réforme du prélèvement à la source "
        "et de la bascule cotisations/CSG"
    ),
    24: (  # LF 2018 (n°2017-1837) / Art. 84, 28
        "L'intention du législateur est d'adapter les dispositions de l'article 39 à la baisse "
        "progressive du taux de l'impôt sur les sociétés et de moderniser les références "
        "aux taux d'intérêt de référence.\n\n"
        "Le législateur souhaite :\n"
        "- Supprimer au 12 bis les références aux taux fractionnaires (18,1/3 et 33,1/3) "
        "liés à l'ancien taux de l'IS, en cohérence avec la trajectoire de baisse du taux à 25 %\n"
        "- Remplacer la référence au 1° bis du III bis de l'article 125 A par celle du premier "
        "alinéa de l'article 124 B, pour actualiser les taux d'intérêt de référence en matière "
        "de charges financières déductibles"
    ),
    25: (  # LF 2019 (n°2018-1317) / Art. 277, 37, 38, 261
        "L'intention du législateur est de réformer en profondeur le régime de limitation "
        "des charges financières déductibles et de transposer la directive ATAD (Anti-Tax "
        "Avoidance Directive) en droit français.\n\n"
        "Le législateur souhaite :\n"
        "- Supprimer l'ancien dispositif du 12 de l'article 39 (premier alinéa) pour le remplacer "
        "par un nouveau régime de plafonnement des charges financières nettes (12 ter)\n"
        "- Transposer l'article 4 de la directive ATAD en instaurant une limitation des intérêts "
        "déductibles fondée sur le ratio EBITDA fiscal\n"
        "- Étendre la notion de liens de dépendance (12 de l'article 39) aux prêts contractés "
        "par des entreprises liées"
    ),
    26: (  # LF 2020 (n°2019-1479) / Art. 190, 58, 69, 228, 74, 82
        "L'intention du législateur est de procéder à de multiples ajustements techniques de "
        "l'article 39, portant sur la prorogation de dispositifs temporaires, l'adaptation des "
        "taxes déductibles au nouveau barème du malus automobile, et la coordination avec d'autres "
        "textes législatifs.\n\n"
        "Le législateur souhaite :\n"
        "- Proroger d'un an (de 2020 à 2021) les dispositifs de suramortissement prévus aux "
        "articles 37 et 39 de lois antérieures\n"
        "- Qualifier en aide à caractère commercial les dotations versées par la SNCF au sens "
        "du 13 de l'article 39, pour sécuriser leur déductibilité\n"
        "- Adapter les références du 4 de l'article 39 au nouveau barème de la taxe sur les "
        "véhicules les plus polluants remplaçant l'article 1010 du CGI\n"
        "- Coordonner avec la réforme de la formation professionnelle (LF 2020, art. 82)"
    ),
    28: (  # LFR 2020 (n°2020-473) / Art. 3 — crise COVID
        "L'intention du législateur est de soutenir les entreprises face à la crise sanitaire "
        "du Covid-19 en rendant non imposables pour le bailleur les abandons et renonciations "
        "de loyers consentis aux entreprises locataires.\n\n"
        "Le législateur souhaite :\n"
        "- Exclure de la base imposable du bailleur les abandons de loyers consentis pendant la crise\n"
        "- Encourager les bailleurs à renoncer aux loyers pour soulager la trésorerie des entreprises "
        "locataires en difficulté\n"
        "- Sécuriser juridiquement le traitement fiscal de ces abandons de créances"
    ),
    29: (  # LFR 2020 (n°2020-935) / Art. 17
        "L'intention du législateur est d'abroger des dispositions temporaires devenues sans objet "
        "dans le cadre du nettoyage législatif opéré par la troisième loi de finances rectificative "
        "pour 2020.\n\n"
        "Le législateur souhaite :\n"
        "- Abroger le II de l'article 39 et plusieurs autres dispositions transitoires arrivées "
        "à échéance\n"
        "- Simplifier le code en supprimant les clauses de caducité et les mesures temporaires "
        "devenues obsolètes"
    ),
    31: (  # LFR 2021 (n°2021-953) / Art. 8
        "L'intention du législateur est de proroger le régime de déductibilité des abandons de "
        "loyers consentis par les bailleurs dans le contexte de la poursuite de la crise sanitaire.\n\n"
        "Le législateur souhaite :\n"
        "- Remplacer la date butoir du 30 juin 2021 par celle du 31 décembre 2021 à l'article 14 B "
        "et au 9° du 1 de l'article 39\n"
        "- Prolonger le dispositif d'incitation aux abandons de loyers mis en place pendant la "
        "crise Covid-19 pour couvrir la période de reprise économique"
    ),
    32: (  # LFR 2022 (n°2022-1157) / Art. 7 — fonds commerciaux
        "L'intention du législateur est d'encadrer la déductibilité de l'amortissement des fonds "
        "commerciaux acquis auprès d'entreprises liées, afin de prévenir les schémas d'optimisation "
        "fiscale intra-groupe.\n\n"
        "Le législateur souhaite :\n"
        "- Exclure de l'amortissement déductible les fonds commerciaux acquis auprès d'une entreprise "
        "liée au sens du 12 de l'article 39, pour éviter la création artificielle de charges "
        "déductibles par des cessions intra-groupe\n"
        "- Compléter le troisième alinéa du 2° du 1 de l'article 39 par cette restriction anti-abus"
    ),
    34: (  # LF 2024 (n°2023-1322) / Art. 110, 67
        "L'intention du législateur est de supprimer le régime temporaire de déductibilité des "
        "abandons de loyers (9° du 1 de l'article 39), devenu sans objet après la fin de la crise "
        "sanitaire, et de coordonner la notion de liens de dépendance avec d'autres dispositifs "
        "fiscaux.\n\n"
        "Le législateur souhaite :\n"
        "- Abroger le 9° du 1 de l'article 39 (abandons de loyers Covid), la mesure temporaire "
        "ayant atteint son terme\n"
        "- Utiliser la définition des liens de dépendance du 12 de l'article 39 comme référence "
        "dans d'autres articles du CGI relatifs aux impositions locales"
    ),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with get_session() as session:
        cache = session.query(AnalysisCache).filter(AnalysisCache.cgi_article == "39").first()
        if not cache:
            print("ERROR: No cache entry for article 39")
            return 1

        analysis = cache.analysis_json
        sections = analysis["sections"]

        # Patch intentions
        patched = 0
        for idx, intention_text in INTENTIONS.items():
            if idx < len(sections):
                old = sections[idx].get("intention", "").strip()
                if not old:
                    sections[idx]["intention"] = intention_text
                    law_ref = sections[idx]["modification"]["law_ref"]
                    print(f"  [{idx}] {law_ref}: filled ({len(intention_text)} chars)")
                    patched += 1
                else:
                    print(f"  [{idx}] already has intention, skipping")

        # Recompute quality metrics
        certitude_scores = {"élevé": 1.0, "moyen": 0.5, "faible": 0.0}
        scores = [certitude_scores.get(s.get("niveau_certitude", "moyen"), 0.5) for s in sections]
        total_sources = sum(len(s.get("sources_an", [])) + len(s.get("sources_senat", [])) for s in sections)
        still_empty = sum(1 for s in sections if not s.get("intention", "").strip())

        print(f"\nPatched: {patched}/{len(INTENTIONS)}")
        print(f"Still empty: {still_empty}/{len(sections)}")
        print(f"Quality score: {int((sum(scores) / len(scores)) * 100)}%")
        print(f"Total sources: {total_sources}")

        if args.dry_run:
            print("\n[DRY RUN] Not saving.")
            return 0

        # Save — must reassign the whole dict for SQLAlchemy to detect the change
        analysis["sections"] = sections
        analysis["metadata"]["generated_date"] = datetime.now(UTC).strftime("%Y-%m-%d")
        # Force SQLAlchemy to detect mutation by reassigning the column
        from sqlalchemy.orm.attributes import flag_modified
        cache.analysis_json = dict(analysis)  # reassign as new dict
        flag_modified(cache, "analysis_json")
        cache.quality_score = int((sum(scores) / len(scores)) * 100)
        cache.sources_count = total_sources
        cache.created_at = datetime.now(UTC).replace(tzinfo=None)
        session.commit()
        print("\nSaved to DB!")

    return 0


if __name__ == "__main__":
    sys.exit(main())
