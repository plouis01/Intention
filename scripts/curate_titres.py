#!/usr/bin/env python3
"""
Curate titres for articles in article_modifications.json.

One-shot curation tool with hard-coded mapping of article refs to titres.
Never overwrites existing non-empty titres.

Usage:
    python scripts/curate_titres.py                # Apply titres
    python scripts/curate_titres.py --dry-run      # Preview only
    python scripts/curate_titres.py --verify        # Check coverage stats
"""

import argparse
import json
import sys
from pathlib import Path

DATA_FILE = Path(__file__).parent.parent / "data" / "article_modifications.json"

# =============================================================================
# TITRE MAPPING - Batch 1: First 200 articles
# =============================================================================
# Conventions:
# - French, concise (5-15 words)
# - Focus on the article's primary purpose
# - Local tax articles: include tax abbreviation (TFPB, TH, CFE, CVAE, IFER, TEOM)
# - Non-CGI articles: prefix with [CODE_NAME]
# - No "Article X du CGI" - redundant with the ref
# =============================================================================

TITRE_MAP = {
    # === IR - Impôt sur le revenu ===
    "4 B": "Domicile fiscal en France",
    "6": "Imposition commune des époux et des personnes à charge",
    "6 D": "Dispositions transitoires relatives à l'imposition des époux",
    "5 D": "Dispositions transitoires relatives aux revenus imposables",
    "11-1": "Rattachement des enfants majeurs au foyer fiscal",
    "11-2": "Rattachement des enfants majeurs mariés ou chargés de famille",
    "13 D": "Dispositions transitoires relatives aux revenus catégoriels",
    "15 D": "Dispositions transitoires relatives aux revenus fonciers",
    "15-6": "Exonération de revenus fonciers pour certains logements",
    "17 D": "Dispositions transitoires relatives à l'évaluation des revenus",
    "17-1": "Evaluation forfaitaire des revenus en nature",
    "27 D": "Dispositions transitoires relatives aux revenus des valeurs mobilières",
    "28 D": "Dispositions transitoires relatives aux revenus agricoles",
    "32-5": "Abattement forfaitaire sur les revenus fonciers (micro-foncier)",
    "34 D": "Dispositions transitoires relatives aux BIC",
    "38": "Détermination du bénéfice imposable (BIC)",
    "44 D": "Dispositions transitoires relatives aux exonérations de BIC",
    "45 D": "Dispositions transitoires relatives aux bénéfices de cession",
    "49 D": "Dispositions transitoires relatives aux sociétés de personnes",
    "83 D": "Dispositions transitoires relatives aux frais professionnels",
    "93": "Détermination des bénéfices imposables des professions non commerciales (BNC)",
    "111-7": "Revenus réputés distribués - prêts aux associés",
    "114": "Déduction des intérêts de comptes courants d'associés",
    "130-1": "Revenus de capitaux mobiliers - base imposable",
    "141-1": "Abattement sur les dividendes perçus par les personnes physiques",
    "146 D": "Dispositions transitoires relatives aux revenus de capitaux mobiliers",
    "150 U": "Plus-values immobilières des particuliers",
    "150-0 B ter": "Report d'imposition des plus-values lors de l'apport de titres à une société contrôlée par l'apporteur (régime de l'apport-cession)",
    "156": "Charges déductibles du revenu global",
    "162-1": "Pensions alimentaires déductibles du revenu global",
    "163 B": "Etalement de l'imposition des revenus exceptionnels (système du quotient)",
    "175": "Obligations déclaratives des contribuables (déclaration de revenus)",
    "182 A": "Retenue à la source sur les salaires versés à des non-résidents",
    "182 A ter": "Retenue à la source sur les gains de levée d'options versés à des non-résidents",
    "182 B": "Retenue à la source sur redevances",
    "193 D": "Dispositions transitoires relatives au quotient familial",
    "195": "Majoration du quotient familial pour les contribuables isolés",
    "199 T": "Réduction d'impôt pour investissement locatif en résidence de tourisme",
    "199 U": "Réduction d'impôt pour investissement locatif social (Scellier)",
    "204 H": "Acompte contemporain de prélèvement à la source (taux par défaut)",

    # === IS - Impôt sur les sociétés ===
    "214-160": "Régime fiscal des sociétés mères - quote-part de frais et charges",
    "217 U": "Amortissement exceptionnel pour investissements productifs",
    "220 Q": "Crédit d'impôt pour dépenses de production cinématographique",
    "220 S": "Crédit d'impôt pour dépenses de production de spectacles vivants",
    "223 A bis": "Intégration fiscale horizontale entre sociétés sœurs",
    "224-1": "Taxe d'apprentissage - assiette et taux",
    "225-115": "Taxe sur les salaires - assiette et barème",
    "233-16": "Obligation de documentation des prix de transfert",
    "241-3": "Acomptes d'impôt sur les sociétés",
    "244 Q": "Crédit d'impôt recherche collaborative",
    "266-2": "Contribution sociale de solidarité des sociétés (C3S) - assiette",

    # === TVA - Taxe sur la valeur ajoutée ===
    "256": "Opérations imposables à la TVA",
    "256 B": "Livraisons de biens à soi-même imposables à la TVA",
    "256 D": "Prestations de services à soi-même imposables",
    "256 E": "Opérations concourant à la production d'un immeuble (LASM)",
    "256 L": "TVA sur les bons à usage unique et à usages multiples",
    "256 O": "TVA sur les services fournis par voie électronique",
    "258": "Lieu de livraison des biens pour la TVA",
    "258 A": "Lieu des livraisons avec transport - TVA intracommunautaire",
    "258 B": "Lieu des acquisitions intracommunautaires",
    "259 D": "Lieu des prestations de services - dérogations particulières",
    "269": "Fait générateur et exigibilité de la TVA",
    "271": "Droit à déduction de la TVA",
    "271 D": "Dispositions transitoires relatives au droit à déduction de TVA",
    "275": "Achats en franchise de TVA (exportateurs)",
    "287": "Obligations déclaratives en matière de TVA",
    "289": "Facturation - mentions obligatoires",
    "289-0": "Règles de facturation applicables aux opérations intracommunautaires",
    "289-0 E": "Facturation électronique obligatoire",
    "289 E": "Factures dématérialisées - conditions d'authenticité",
    "291": "Exonérations de TVA à l'importation",
    "298 S": "TVA applicable aux opérations sur l'or",

    # === Droits d'enregistrement et succession ===
    "746": "Droits de partage",
    "723-1": "Droit de mutation sur cessions de parts de SCI",
    "752-1": "Présomption de propriété pour les droits de succession",
    "790 G": "Donation avec réserve d'usufruit - abattements spécifiques",
    "794": "Exonérations de droits de mutation à titre gratuit",
    "800": "Obligations déclaratives en matière de succession",
    "885-0 V bis": "Réduction d'ISF pour investissement dans les PME",

    # === Taxes indirectes et droits sur les boissons et alcools ===
    "301-2": "Droits de circulation sur les vins et cidres",
    "303-2": "Droit de consommation sur les produits intermédiaires (vins doux naturels)",
    "312-1": "Taxe sur les premixes et boissons sucrées alcoolisées",
    "321-1": "Droits sur les bières et boissons non alcoolisées",
    "341-4": "Cotisation de sécurité sociale sur les boissons alcooliques",
    "365-1": "Capsules représentatives de droits sur les alcools",
    "411-2": "Régime fiscal de la production d'alcool",
    "411-27": "Contingents d'alcool de bouche",
    "415": "Droits de fabrication sur les alcools",
    "452-6": "Droit de licence sur les débitants de boissons",
    "511-1": "Garantie et contrôle des ouvrages en métaux précieux",
    "511-6": "Contributions sur les métaux précieux",

    # === Pénalités et recouvrement ===
    "1635-0 Q": "Droit fixe de procédure (contribution pour l'aide juridique)",
    "1671 A": "Retenue à la source - obligations de versement",
    "1691 B": "Solidarité de paiement entre époux pour l'impôt sur le revenu",
    "1729 B": "Majoration pour manquement délibéré en matière de prix de transfert",
    "1736 D": "Amende pour défaut de déclaration de comptes à l'étranger (dispositions transitoires)",
    "1740 D": "Sanctions pour défaut de facturation électronique (dispositions transitoires)",
    "1810": "Pénalités en matière de contributions indirectes",

    # === Taxe foncière sur les propriétés bâties (TFPB) ===
    "1379": "Répartition de la taxe foncière entre les collectivités",
    "1379 E": "Répartition de la TFPB - dispositions transitoires",
    "1382": "Exonérations permanentes de taxe foncière sur les propriétés bâties",
    "1382 C bis": "Exonération de TFPB pour les installations de méthanisation agricole",
    "1382 E": "Exonération de TFPB pour les bâtiments ruraux affectés à usage agricole",
    "1382 F": "Exonération de TFPB pour les outillages et installations des établissements industriels",
    "1382 H": "Exonération de TFPB pour les locaux des organismes HLM en QPV",
    "1382 İ": "Exonération de TFPB pour les immeubles situés en zones franches urbaines",
    "1383": "Exonérations temporaires de TFPB pour les constructions nouvelles",
    "1383-0 B": "Exonération de TFPB pour les logements économes en énergie",
    "1383 D": "Exonération de TFPB pour les logements faisant l'objet de travaux d'économie d'énergie",
    "1383 E": "Exonération de TFPB pour les logements en QPV ayant fait l'objet de travaux",
    "1383 F": "Exonération de TFPB pour les locaux à usage de résidence principale en ZRR",
    "1383 J": "Exonération de TFPB pour les logements neufs à haute performance énergétique",
    "1384 A": "Exonération temporaire de TFPB pour les logements sociaux",
    "1384 C": "Exonération de TFPB pour les logements acquis et améliorés par les organismes HLM",
    "1384 F": "Exonération de TFPB pour les logements intermédiaires",
    "1391 B ter": "Dégrèvement de TFPB pour les organismes HLM en QPV",

    # === Taxe foncière sur les propriétés non bâties (TFPNB) ===
    "1396": "Base d'imposition et majoration de la taxe foncière sur les propriétés non bâties",
    "1396 E": "Majoration de la TFPNB en zone tendue (dispositions transitoires)",

    # === Taxe d'habitation (TH) ===
    "1406": "Obligations déclaratives des propriétaires pour la taxe d'habitation",
    "1407": "Champ d'application de la taxe d'habitation",
    "1407 E": "Taxe d'habitation - dispositions transitoires liées à la réforme",
    "1407 T": "Taxe d'habitation - dispositions transitoires",
    "1408": "Personnes imposables à la taxe d'habitation",
    "1409,1411 et 1649": "Base d'imposition de la taxe d'habitation et commissions communales",
    "1411": "Abattements applicables à la base de la taxe d'habitation",
    "1411 D": "Abattements TH - dispositions transitoires",
    "1411 E": "Abattements TH - dispositions transitoires liées à la suppression",
    "1413": "Plafonnement de la taxe d'habitation en fonction du revenu",
    "1414": "Exonérations de taxe d'habitation",
    "1414 B": "Plafonnement de la TH en fonction du revenu (mécanisme antérieur)",
    "1414 C": "Dégrèvement progressif de la taxe d'habitation sur la résidence principale",
    "1414 D": "Dégrèvement de TH pour les contribuables de condition modeste",
    "1414 E": "Suppression de la taxe d'habitation sur les résidences principales",
    "1417": "Conditions de revenus pour les exonérations et dégrèvements d'impôts locaux",
    "1417 D": "Conditions de revenus - dispositions transitoires",
    "1417 E": "Conditions de revenus - dispositions transitoires liées à la réforme",

    # === CFE - Cotisation foncière des entreprises ===
    "1458": "Exonérations de cotisation foncière des entreprises",
    "1464 F": "Exonération de CFE pour les entreprises en ZRD (zones de restructuration de la défense)",
    "1464 G": "Exonération de CFE pour les jeunes entreprises innovantes",
    "1466 A": "Exonération de CFE dans les zones franches urbaines",
    "1466 F": "Exonération de CFE dans les bassins d'emploi à redynamiser",
    "1468": "Base minimum de cotisation foncière des entreprises",
    "1468 D": "Base minimum de CFE - dispositions transitoires",

    # === Valeurs locatives et évaluation ===
    "1494": "Evaluation de la valeur locative des propriétés bâties",
    "1496 et 1497": "Evaluation par comparaison de la valeur locative des locaux d'habitation et professionnels",
    "1497": "Evaluation de la valeur locative des locaux professionnels",
    "1498 A": "Evaluation de la valeur locative des locaux professionnels - secteurs d'évaluation",
    "1499-00 A": "Evaluation de la valeur locative des établissements industriels - méthode comptable",
    "1502": "Commission départementale des valeurs locatives",
    "1504": "Sous-commissions communales des impôts directs",
    "1507": "Commission communale des impôts directs - rôle pour les locaux professionnels",
    "1518 A quinquies": "Mise à jour annuelle des valeurs locatives des locaux professionnels",
    "1518 A sexies": "Coefficient de neutralisation pour la révision des valeurs locatives",
    "1518 E": "Mise à jour des valeurs locatives - dispositions transitoires",
    "1518 T": "Valeurs locatives - dispositions transitoires de la révision",

    # === Taux, répartition et frais d'assiette ===
    "1522": "Taxe d'enlèvement des ordures ménagères (TEOM)",
    "1522 E": "TEOM - dispositions transitoires",
    "1530 B": "Taxe pour la gestion des milieux aquatiques et prévention des inondations (GEMAPI)",
    "1586 A": "CVAE - champ d'application et assujettissement",
    "1586 N": "CVAE - détermination de la valeur ajoutée",
    "1586 T": "CVAE - dispositions transitoires",
    "1609 B": "Taxe spéciale d'équipement au profit de l'Île-de-France",
    "1609 G": "Taxe spéciale d'équipement au profit d'autres établissements publics fonciers",
    "1609 Q": "Taxe additionnelle sur les droits de mutation au profit de l'Île-de-France",
    "1636 B octies": "Fixation des taux de la CFE et de la CVAE",
    "1636 B septies": "Règles de lien entre les taux des impôts locaux",
    "1636 B sexies": "Fixation des taux de la taxe d'habitation et des taxes foncières",
    "1636 B decies": "Fixation des taux des taxes additionnelles",
    "1638 E": "Harmonisation des taux lors de fusions de communes (dispositions transitoires)",
    "1638-0 B": "Intégration fiscale progressive lors de fusions d'EPCI",
    "1638 Q": "Harmonisation des taux lors de fusions d'EPCI (dispositions transitoires)",
    "1639 A bis": "Délibérations des collectivités en matière de fiscalité directe locale",
    "1639 A ter": "Délibérations des collectivités - exonérations facultatives",
    "1639 A quater": "Délibérations des collectivités - abattements de taxe d'habitation",
    "1640": "Compensation des pertes de recettes des collectivités",
    "1640 S": "Compensation des pertes de recettes - dispositions transitoires",
    "1641": "Frais d'assiette et de recouvrement des impôts locaux",
    "1641 E": "Frais d'assiette - dispositions transitoires",
    "1645": None,  # placeholder - not in first 200
    "1647": "Plafonnement de la contribution économique territoriale (CET)",
    "1650": "Commission communale des impôts directs",
    "1650 B": "Commission intercommunale des impôts directs",
    "1650 C": "Commission départementale des impôts directs",
    "1655 S": "Groupements de collectivités - dispositions fiscales particulières",
    "1656": "Dispositions communes aux impôts directs locaux",
    "1656 B": "Dispositions communes aux impôts directs locaux - dispositions transitoires",

    # === IFER - Imposition forfaitaire sur les entreprises de réseaux ===
    "1519 C": "IFER sur les installations de production d'électricité d'origine nucléaire ou thermique",
    "1519 F": "IFER sur les installations de production d'électricité d'origine photovoltaïque ou hydraulique",
    "1519 H": "IFER sur les antennes relais de téléphonie mobile",
    "1519 İ": "IFER sur les transformateurs électriques",

    # === Non-CGI articles ===
    # CGCT - Code général des collectivités territoriales
    "2333-41": "[CGCT] Taxe de séjour - tarifs et modalités de recouvrement",
    "2333-34": "[CGCT] Taxe locale sur la publicité extérieure",

    # CIBS - Code des impositions sur les biens et services
    "4430-1": "[CIBS] Contribution sur les boissons sucrées",
    "4431-1": "[CIBS] Contribution sur les boissons édulcorées",
    "4432-5": "[CIBS] Tarifs de la contribution sur les boissons sucrées et édulcorées",

    # Code du travail
    "5132-7": "[Code du travail] Aide à l'insertion par l'activité économique",
    "5343-6": "[Code du travail] Financement de la formation professionnelle",
    "5422-13": "[Code du travail] Financement de l'assurance chômage",
    "5424-20": "[Code du travail] Allocation de solidarité spécifique (ASS)",
    "5427-1": "[Code du travail] Bonus-malus sur les contributions patronales d'assurance chômage",

    # === Autres articles CGI du premier lot ===
    "1242-2": "Obligations de retenue à la source sur les produits de placement",
}

# Remove None placeholders
TITRE_MAP = {k: v for k, v in TITRE_MAP.items() if v is not None}


def load_data():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def apply_titres(data, dry_run=False):
    articles = data["articles"]
    stats = {"set": 0, "skipped_existing": 0, "missing": 0}
    changes = []

    for ref, titre in sorted(TITRE_MAP.items()):
        if ref not in articles:
            stats["missing"] += 1
            changes.append(f"  MISSING  {ref!r} - not found in article_modifications.json")
            continue

        existing = articles[ref].get("titre", "")
        if existing:
            stats["skipped_existing"] += 1
            changes.append(f"  SKIP     {ref!r} - already has titre: {existing!r}")
            continue

        stats["set"] += 1
        changes.append(f"  SET      {ref!r} → {titre!r}")
        if not dry_run:
            articles[ref]["titre"] = titre

    return stats, changes


def verify_coverage(data):
    articles = data["articles"]
    total = len(articles)
    with_titre = sum(1 for a in articles.values() if a.get("titre"))
    without_titre = total - with_titre

    print(f"\n=== Coverage Report ===")
    print(f"Total articles:       {total}")
    print(f"With titre:           {with_titre} ({100*with_titre/total:.1f}%)")
    print(f"Without titre:        {without_titre} ({100*without_titre/total:.1f}%)")
    print(f"TITRE_MAP entries:    {len(TITRE_MAP)}")

    # Check how many TITRE_MAP entries are in the first 200
    first_200 = list(articles.keys())[:200]
    mapped_in_200 = sum(1 for k in first_200 if k in TITRE_MAP)
    titled_in_200 = sum(1 for k in first_200 if articles[k].get("titre"))
    print(f"\nFirst 200 articles:")
    print(f"  Mapped in TITRE_MAP: {mapped_in_200}")
    print(f"  With titre (total):  {titled_in_200}")
    print(f"  Coverage:            {100*titled_in_200/200:.1f}%")

    # List first-200 articles without titre
    untitled = [k for k in first_200 if not articles[k].get("titre") and k not in TITRE_MAP]
    if untitled:
        print(f"\n  Untitled & unmapped ({len(untitled)}):")
        for k in untitled:
            print(f"    {k}")


def main():
    parser = argparse.ArgumentParser(description="Curate titres for article_modifications.json")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    parser.add_argument("--verify", action="store_true", help="Show coverage statistics")
    args = parser.parse_args()

    data = load_data()

    if args.verify:
        verify_coverage(data)
        return

    mode = "DRY RUN" if args.dry_run else "APPLY"
    print(f"\n=== Curate Titres ({mode}) ===")
    print(f"TITRE_MAP entries: {len(TITRE_MAP)}")

    stats, changes = apply_titres(data, dry_run=args.dry_run)

    for line in changes:
        print(line)

    print(f"\n--- Summary ---")
    print(f"Set:              {stats['set']}")
    print(f"Skipped (existing): {stats['skipped_existing']}")
    print(f"Missing:          {stats['missing']}")

    if not args.dry_run and stats["set"] > 0:
        save_data(data)
        print(f"\nSaved to {DATA_FILE}")

        # Verify
        verify_coverage(data)
    elif args.dry_run:
        print(f"\nDry run - no changes written.")


if __name__ == "__main__":
    main()
