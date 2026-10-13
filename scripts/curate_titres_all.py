#!/usr/bin/env python3
"""
Comprehensive titre curation for ALL articles in article_modifications.json.

Combines:
1. Manual TITRE_MAP for well-known articles
2. Smart generator using CGI structure, suffix rules, and article context

Usage:
    python scripts/curate_titres_all.py                # Apply all titres
    python scripts/curate_titres_all.py --dry-run      # Preview only
    python scripts/curate_titres_all.py --verify        # Coverage stats
    python scripts/curate_titres_all.py --stats         # Show generation method stats
"""

import argparse
import json
import re
from pathlib import Path

DATA_FILE = Path(__file__).parent.parent / "data" / "article_modifications.json"

# =============================================================================
# CGI SECTION IDENTIFICATION
# =============================================================================
# Maps article number ranges to their CGI section topic.
# Used as fallback when no manual titre is available.
# Order matters: first match wins.
# =============================================================================

CGI_SECTIONS = [
    # Titre I - Impôt sur le revenu
    (1, 1, "Principe de l'impôt sur le revenu"),
    (2, 2, "Mise en recouvrement de l'IR"),
    (3, 3, "Personnes imposables à l'IR"),
    (4, 4, "Lieu d'imposition"),
    (5, 5, "Revenus imposables des non-résidents"),
    (6, 6, "Imposition commune des époux"),
    (7, 9, "Déclaration et contrôle de l'IR"),
    (10, 10, "Plafonnement global des avantages fiscaux"),
    (11, 11, "Quotient familial - nombre de parts"),
    (12, 13, "Revenu imposable - définition et catégories"),
    (14, 33, "Revenus fonciers"),
    (34, 36, "BIC - définition et champ d'application"),
    (37, 37, "Réévaluation libre du bilan"),
    (38, 38, "BIC - détermination du bénéfice imposable"),
    (39, 39, "BIC - charges déductibles"),
    (40, 43, "BIC - plus-values et résultat"),
    (44, 44, "BIC - exonérations zonées"),
    (45, 49, "BIC - sociétés de personnes et résultat"),
    (50, 54, "BIC - obligations déclaratives"),
    (55, 61, "BIC - vérification et forfait"),
    (62, 62, "Rémunération des dirigeants (article 62)"),
    (63, 64, "Bénéfices agricoles - définition et évaluation"),
    (65, 68, "Bénéfices agricoles - évaluation et régimes"),
    (69, 71, "Bénéfices agricoles - régime réel"),
    (72, 72, "Plus-values agricoles"),
    (73, 73, "Déduction pour épargne de précaution"),
    (74, 78, "Bénéfices agricoles - dispositions spéciales"),
    (79, 79, "Traitements et salaires - définition"),
    (80, 80, "Traitements et salaires - indemnités imposables"),
    (81, 81, "Exonérations de traitements et salaires"),
    (82, 82, "Avantages en nature"),
    (83, 83, "Frais professionnels déductibles"),
    (84, 91, "Traitements et salaires - dispositions diverses"),
    (92, 92, "BNC - définition"),
    (93, 93, "BNC - détermination du bénéfice"),
    (94, 103, "BNC - régimes et dispositions diverses"),
    (104, 107, "Rentes viagères"),
    (108, 115, "Revenus de capitaux mobiliers - produits de placements"),
    (116, 125, "Revenus de capitaux mobiliers - régimes spéciaux"),
    (126, 134, "Revenus de capitaux mobiliers - dispositions communes"),
    (135, 149, "Revenus et bénéfices - dispositions communes"),
    (150, 155, "Plus-values des particuliers"),
    (156, 163, "Charges déductibles du revenu global"),
    (164, 168, "Revenu global - dispositions diverses"),
    (169, 169, "Contribuables transférant leur domicile hors de France"),
    (170, 175, "Obligations déclaratives des contribuables"),
    (176, 181, "Contrôle des déclarations"),
    (182, 192, "Retenue à la source"),
    (193, 198, "Quotient familial et calcul de l'impôt"),
    (199, 199, "Réductions d'impôt sur le revenu"),
    (200, 200, "Crédits d'impôt sur le revenu"),
    (201, 204, "Prélèvement à la source et acompte"),
    # Titre II - IS
    (205, 208, "Impôt sur les sociétés - champ d'application"),
    (209, 214, "IS - détermination du résultat fiscal"),
    (215, 219, "IS - calcul et taux"),
    (220, 220, "IS - crédits d'impôt"),
    (221, 223, "IS - intégration fiscale et dispositions diverses"),
    # Titre III - Taxes sur les salaires
    (224, 224, "Taxe d'apprentissage"),
    (225, 225, "Taxe sur les salaires"),
    (226, 230, "Participation des employeurs"),
    (231, 231, "Taxe sur les salaires - assiette et calcul"),
    (232, 235, "Taxes assises sur les salaires - dispositions diverses"),
    # Dispositions communes IR/IS
    (236, 244, "Dispositions communes IR/IS"),
    (237, 239, "Obligations comptables et fiscales"),
    (238, 238, "Dispositifs anti-abus et obligations déclaratives"),
    (240, 244, "Dispositions communes - retenues et crédits d'impôt"),
    (245, 255, "Dispositions communes - revenus étrangers"),
    # Titre IV - TVA
    (256, 257, "TVA - opérations imposables"),
    (258, 259, "TVA - territorialité"),
    (260, 263, "TVA - exonérations"),
    (264, 270, "TVA - base d'imposition et fait générateur"),
    (271, 277, "TVA - déductions"),
    (278, 281, "TVA - taux"),
    (282, 286, "TVA - régimes spéciaux et obligations"),
    (287, 289, "TVA - obligations déclaratives et facturation"),
    (290, 293, "TVA - régimes particuliers"),
    (294, 296, "TVA - DOM-TOM"),
    (297, 298, "TVA - dispositions diverses"),
    # Titre V - Contributions indirectes
    (299, 310, "Contributions indirectes - droits de circulation"),
    (311, 320, "Contributions indirectes - boissons"),
    (321, 340, "Contributions indirectes - bières et boissons non alcoolisées"),
    (341, 365, "Contributions indirectes - alcools et spiritueux"),
    (366, 410, "Contributions indirectes - production et distribution"),
    (411, 450, "Contributions indirectes - régime des alcools"),
    (451, 460, "Contributions indirectes - licences de débit"),
    (461, 520, "Contributions indirectes - dispositions diverses"),
    (521, 564, "Contributions indirectes - garantie des métaux précieux"),
    (565, 575, "Tabacs - fabrication et distribution"),
    (576, 614, "Tabacs et allumettes - dispositions fiscales"),
    # Droits d'enregistrement
    (615, 634, "Droits d'enregistrement - dispositions générales"),
    (635, 656, "Droits d'enregistrement - formalités et délais"),
    (657, 664, "Droits d'enregistrement - pénalités de retard"),
    (665, 680, "Droits d'enregistrement - évaluation des biens"),
    (681, 700, "Droits d'enregistrement - tarifs"),
    (701, 725, "DMTO - mutations à titre onéreux d'immeubles"),
    (726, 735, "DMTO - cessions de droits sociaux"),
    (736, 750, "DMTO - autres mutations"),
    (751, 775, "DMTG - successions"),
    (776, 795, "DMTG - donations et abattements"),
    (796, 810, "DMTG - exonérations et réductions"),
    (811, 850, "Droits d'enregistrement - dispositions diverses"),
    (851, 870, "Droits d'enregistrement - régimes spéciaux"),
    # Timbre
    (871, 999, "Droits de timbre et impositions assimilées"),
    # ISF/IFI
    (885, 885, "Impôt sur la fortune (ISF/IFI)"),
    (900, 999, "Droits de timbre - dispositions diverses"),
    (953, 999, "Droits de timbre - actes et conventions"),
    # Contributions diverses
    (1000, 1023, "Impôts et taxes divers"),
    (1024, 1060, "Dispositions communes aux impôts"),
    (1061, 1133, "Régimes spéciaux et exonérations"),
    (1134, 1378, "Dispositions communes et transitoires"),
    # Impositions directes locales
    (1379, 1381, "IDL - répartition et champ d'application"),
    (1382, 1389, "TFPB - exonérations"),
    (1390, 1391, "TFPB - dégrèvements"),
    (1392, 1396, "TFPNB - exonérations et base"),
    (1393, 1395, "TFPNB - exonérations temporaires"),
    (1396, 1396, "TFPNB - majoration en zone tendue"),
    (1397, 1406, "Taxes foncières - dispositions communes"),
    (1407, 1418, "Taxe d'habitation"),
    (1419, 1446, "Taxe d'habitation et impositions annexes"),
    (1447, 1478, "CFE - cotisation foncière des entreprises"),
    (1479, 1519, "Valeurs locatives et évaluation"),
    (1519, 1523, "IFER et TEOM"),
    (1524, 1535, "Taxes annexes locales"),
    (1536, 1599, "Taxes et impositions locales diverses"),
    (1586, 1586, "CVAE - cotisation sur la valeur ajoutée"),
    (1594, 1594, "DMTO - taxe de publicité foncière"),
    (1599, 1599, "Taxes additionnelles locales"),
    (1600, 1609, "Taxes au profit d'organismes divers"),
    (1610, 1635, "Taxes locales - dispositions diverses"),
    (1636, 1641, "Taux et frais d'assiette des impôts locaux"),
    (1642, 1656, "IDL - dispositions communes"),
    # Recouvrement et pénalités
    (1657, 1680, "Recouvrement des impôts directs"),
    (1681, 1700, "Recouvrement - modalités de paiement"),
    (1701, 1725, "Recouvrement - privilèges et sûretés"),
    (1726, 1740, "Pénalités et sanctions fiscales"),
    (1741, 1760, "Poursuites et sanctions pénales"),
    (1761, 1800, "Procédures de contrôle et contentieux"),
    (1801, 1840, "Contributions indirectes - pénalités et poursuites"),
    # Autres
    (1841, 1920, "Dispositions transitoires et finales"),
    (1921, 1999, "Dispositions diverses"),
    (2000, 2100, "Dispositions relatives aux collectivités"),
]

# =============================================================================
# NON-CGI CODE IDENTIFICATION
# =============================================================================
# For articles with base numbers > 2000, identifies the likely source code.
# =============================================================================

NON_CGI_CODES = [
    # CGCT - Code général des collectivités territoriales
    (2101, 2199, "CGCT", "Communes - dispositions générales"),
    (2111, 2199, "CGCT", "Communes - administration"),
    (2212, 2299, "CGCT", "Communes - finances"),
    (2213, 2213, "CGCT", "Pouvoirs de police du maire"),
    (2221, 2299, "CGCT", "Communes - gestion financière"),
    (2311, 2399, "CGCT", "Communes - recettes fiscales"),
    (2321, 2321, "CGCT", "Dépenses obligatoires de la commune"),
    (2332, 2335, "CGCT", "Communes - recettes fiscales"),
    (2333, 2333, "CGCT", "Taxes communales"),
    (2393, 2399, "CGCT", "Communes - dispositions financières diverses"),
    (2512, 2599, "CGCT", "Départements - finances"),
    (2531, 2531, "CGCT", "Départements - fiscalité"),
    # CGCT départements et régions
    (3111, 3199, "CGCT", "Départements - dispositions générales"),
    (3211, 3299, "CGCT", "Départements - finances"),
    (3311, 3399, "CGCT", "Régions - dispositions générales"),
    (3411, 3499, "CGCT", "Régions - finances"),
    (3512, 3599, "CGCT", "Coopération intercommunale - finances"),
    (3543, 3599, "CGCT", "Coopération intercommunale - fiscalité"),
    (3662, 3699, "CGCT", "Collectivités - dispositions financières"),
    (3822, 3899, "CGCT", "Collectivités - dispositions diverses"),
    # CIBS - Code des impositions sur les biens et services
    (4000, 4099, "CIBS", "Dispositions générales"),
    (4011, 4012, "CIBS", "Accises - dispositions communes"),
    (4111, 4199, "CIBS", "Accises - énergies"),
    (4122, 4131, "CIBS", "Accises - produits énergétiques"),
    (4211, 4299, "CIBS", "Accises - tabacs"),
    (4251, 4299, "CIBS", "Accises - tabacs - tarifs"),
    (4301, 4399, "CIBS", "Accises - alcools"),
    (4310, 4316, "CIBS", "Accises - alcools - champ et tarifs"),
    (4414, 4499, "CIBS", "Taxes sur les boissons non alcooliques"),
    (4422, 4432, "CIBS", "Taxes sur les boissons sucrées et édulcorées"),
    (4623, 4699, "CIBS", "Taxes sectorielles"),
    # Code du travail
    (5114, 5199, "Code du travail", "Emploi - aides et dispositifs"),
    (5121, 5132, "Code du travail", "Insertion par l'activité économique"),
    (5211, 5299, "Code du travail", "Contrats aidés et dispositifs d'emploi"),
    (5311, 5399, "Code du travail", "Services à la personne et formation"),
    (5421, 5499, "Code du travail", "Assurance chômage"),
    (5511, 5599, "Code du travail", "Formation professionnelle"),
    (5711, 5799, "Code du travail", "Travailleurs handicapés"),
    (5843, 5899, "Code du travail", "Dispositions diverses"),
    # Code de la sécurité sociale
    (6111, 6199, "CSS", "Organisation de la sécurité sociale"),
    (6211, 6299, "CSS", "Cotisations et contributions sociales"),
    (6311, 6399, "CSS", "Prestations familiales et sociales"),
    (6412, 6499, "CSS", "Assurance maladie"),
    (6527, 6599, "CSS", "Prestations diverses"),
    (6611, 6699, "CSS", "Retraite et vieillesse"),
    # Code de la construction et de l'habitation
    (6500, 6500, "CCH", "Construction et habitation"),
    # Code de commerce
    (7121, 7199, "Code de commerce", "Sociétés commerciales"),
    (7221, 7299, "Code de commerce", "Commissaires aux comptes et audit"),
    (7342, 7399, "Code de commerce", "Registre du commerce"),
    (7613, 7699, "Code de commerce", "Procédures collectives"),
    # Code monétaire et financier
    (8211, 8299, "CMF", "Marchés financiers et intermédiaires"),
]

# =============================================================================
# SUFFIX RULES
# =============================================================================

ANNEXE_SUFFIX_MAP = {
    "D": "annexe II du CGI",
    "E": "annexe III du CGI",
    "S": "annexe IV du CGI",
}

# =============================================================================
# MANUAL TITRE MAP - Well-known CGI articles
# =============================================================================
# Loaded from separate data file to keep this script manageable.
# =============================================================================

def load_titre_map():
    """Load manual titre mappings from the data module."""
    from curate_titres_map import TITRE_MAP
    return TITRE_MAP


# =============================================================================
# TITRE GENERATOR
# =============================================================================

def parse_article_ref(ref: str):
    """Parse an article reference into components.

    Returns dict with:
        base_num: int or None
        suffix: str (e.g., 'D', 'E', 'bis', 'ter A', etc.)
        sub_num: int or None (for dash-number patterns like 10-3)
        is_combined: bool
        raw: str
    """
    ref = ref.strip()
    result = {
        "raw": ref,
        "base_num": None,
        "suffix": "",
        "sub_num": None,
        "is_combined": False,
        "parent_ref": None,
    }

    # Combined articles (e.g., "12,13,28 et 31")
    if "," in ref or " et " in ref:
        result["is_combined"] = True
        m = re.match(r"(\d+)", ref)
        if m:
            result["base_num"] = int(m.group(1))
        return result

    # Numeric-dash pattern (e.g., "10-3", "302-5")
    m = re.match(r"^(\d+)-(\d+)$", ref)
    if m:
        result["base_num"] = int(m.group(1))
        result["sub_num"] = int(m.group(2))
        return result

    # Complex dash pattern (e.g., "150-0 B ter", "1383-0 B")
    m = re.match(r"^(\d+)-(.+)$", ref)
    if m:
        result["base_num"] = int(m.group(1))
        result["suffix"] = m.group(2).strip()
        result["parent_ref"] = str(result["base_num"])
        return result

    # Plain number
    m = re.match(r"^(\d+)$", ref)
    if m:
        result["base_num"] = int(m.group(1))
        return result

    # Number + suffix (e.g., "39 bis A", "182 A", "44 octies")
    m = re.match(r"^(\d+)\s+(.+)$", ref)
    if m:
        result["base_num"] = int(m.group(1))
        result["suffix"] = m.group(2).strip()
        result["parent_ref"] = str(result["base_num"])
        return result

    return result


def get_cgi_section(base_num: int) -> str:
    """Get CGI section title for an article number."""
    if base_num is None:
        return ""
    # Search in reverse order to get most specific match first
    best = ""
    for start, end, title in CGI_SECTIONS:
        if start <= base_num <= end:
            best = title
    return best


def get_non_cgi_info(base_num: int) -> tuple:
    """Get non-CGI code info for an article number > 2000.

    Returns (code_name, section_title) or (None, None).
    """
    if base_num is None or base_num < 2000:
        return None, None
    best_code, best_section = None, None
    for start, end, code, section in NON_CGI_CODES:
        if start <= base_num <= end:
            best_code = code
            best_section = section
    return best_code, best_section


def get_parent_titre(ref: str, parsed: dict, titre_map: dict) -> str:
    """Try to find the parent article's titre."""
    if parsed["parent_ref"] and parsed["parent_ref"] in titre_map:
        return titre_map[parsed["parent_ref"]]
    if parsed["base_num"] is not None:
        base_str = str(parsed["base_num"])
        if base_str in titre_map:
            return titre_map[base_str]
    return ""


def generate_titre(ref: str, article_data: dict, titre_map: dict) -> str:
    """Generate a titre for an article using all available context."""
    parsed = parse_article_ref(ref)
    base_num = parsed["base_num"]
    suffix = parsed["suffix"]

    if base_num is None:
        return ""

    # --- Non-CGI articles (base > 2000, except known CGI ranges) ---
    if base_num >= 2000 and base_num not in range(2044, 2045):
        code_name, section = get_non_cgi_info(base_num)
        if code_name:
            if parsed["sub_num"] is not None:
                return f"[{code_name}] {section}"
            elif suffix:
                return f"[{code_name}] {section}"
            else:
                return f"[{code_name}] {section}"
        # Unknown code
        if parsed["sub_num"] is not None:
            return f"[Autre code] Disposition modifiée par une loi de finances"
        return f"[Autre code] Disposition modifiée par une loi de finances"

    # --- Annexe suffix (D, E, S) ---
    # Single letter suffix that's an annexe designation
    if suffix in ANNEXE_SUFFIX_MAP:
        annexe_label = ANNEXE_SUFFIX_MAP[suffix]
        parent_titre = get_parent_titre(ref, parsed, titre_map)
        if parent_titre:
            # Truncate long parent titles
            if len(parent_titre) > 60:
                parent_titre = parent_titre[:57] + "..."
            return f"{parent_titre} ({annexe_label})"
        section = get_cgi_section(base_num)
        if section:
            return f"{section} ({annexe_label})"
        return f"Dispositions réglementaires ({annexe_label})"

    # Compound annexe suffix (e.g., "38 sexdecies D", "72 D bis")
    if suffix and suffix[-1] in "DES" and suffix[-2:] in (" D", " E", " S"):
        annexe_letter = suffix[-1]
        if annexe_letter in ANNEXE_SUFFIX_MAP:
            annexe_label = ANNEXE_SUFFIX_MAP[annexe_letter]
            parent_titre = get_parent_titre(ref, parsed, titre_map)
            if parent_titre:
                return f"{parent_titre} ({annexe_label})"
            section = get_cgi_section(base_num)
            if section:
                return f"{section} ({annexe_label})"

    # --- Other single letter suffixes (A, B, C, etc.) ---
    if re.match(r"^[A-Z]$", suffix) and suffix not in "DES":
        parent_titre = get_parent_titre(ref, parsed, titre_map)
        if parent_titre:
            return f"{parent_titre} - {suffix}"
        section = get_cgi_section(base_num)
        if section:
            return f"{section} - {suffix}"

    # --- Latin ordinal suffixes (bis, ter, quater, etc.) ---
    latin_ordinals = (
        "bis", "ter", "quater", "quinquies", "sexies", "septies",
        "octies", "nonies", "decies", "undecies", "duodecies",
        "terdecies", "quaterdecies", "quindecies", "sexdecies",
    )
    if suffix:
        first_word = suffix.split()[0].lower()
        if first_word in latin_ordinals or any(o in suffix.lower() for o in latin_ordinals):
            parent_titre = get_parent_titre(ref, parsed, titre_map)
            if parent_titre:
                return f"{parent_titre} ({suffix})"
            section = get_cgi_section(base_num)
            if section:
                return f"{section} ({suffix})"

    # --- Suffixes with special meaning (T, Q, N, O, P) ---
    special_suffixes = {
        "T": "dispositions transitoires",
        "Q": "dispositions spécifiques",
        "N": "dispositions nouvelles",
        "O": "dispositions optionnelles",
        "P": "dispositions particulières",
    }
    if suffix in special_suffixes:
        parent_titre = get_parent_titre(ref, parsed, titre_map)
        desc = special_suffixes[suffix]
        if parent_titre:
            return f"{parent_titre} - {desc}"
        section = get_cgi_section(base_num)
        if section:
            return f"{section} - {desc}"

    # --- Numeric-dash sub-articles ---
    if parsed["sub_num"] is not None:
        section = get_cgi_section(base_num)
        if section:
            return section

    # --- Combined articles ---
    if parsed["is_combined"]:
        section = get_cgi_section(base_num)
        if section:
            return f"{section} (articles combinés)"
        return "Dispositions combinées"

    # --- Complex suffix patterns ---
    if suffix:
        parent_titre = get_parent_titre(ref, parsed, titre_map)
        if parent_titre:
            return f"{parent_titre} ({suffix})"
        section = get_cgi_section(base_num)
        if section:
            return f"{section} ({suffix})"

    # --- Plain number fallback ---
    section = get_cgi_section(base_num)
    if section:
        return section

    return ""


# =============================================================================
# MAIN LOGIC
# =============================================================================

def load_data():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def apply_all_titres(data, titre_map, dry_run=False):
    """Apply titres to all articles: manual map first, then generator."""
    articles = data["articles"]
    stats = {"manual": 0, "generated": 0, "skipped_existing": 0, "failed": 0}
    changes = []

    for ref in articles:
        existing = articles[ref].get("titre", "")
        if existing:
            stats["skipped_existing"] += 1
            continue

        # Try manual map first
        if ref in titre_map:
            titre = titre_map[ref]
            stats["manual"] += 1
            method = "MANUAL"
        else:
            # Try generator
            titre = generate_titre(ref, articles[ref], titre_map)
            if titre:
                stats["generated"] += 1
                method = "GEN"
            else:
                stats["failed"] += 1
                method = "FAIL"
                changes.append(f"  {method:6s} {ref!r}")
                continue

        if not dry_run:
            articles[ref]["titre"] = titre

        if dry_run:
            changes.append(f"  {method:6s} {ref!r:40s} → {titre!r}")

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

    if without_titre > 0 and without_titre <= 50:
        print(f"\nUntitled articles:")
        for ref, art in articles.items():
            if not art.get("titre"):
                print(f"  {ref}")


def main():
    parser = argparse.ArgumentParser(description="Curate ALL titres")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--show-failures", action="store_true")
    parser.add_argument("--show-generated", action="store_true")
    args = parser.parse_args()

    data = load_data()

    if args.verify:
        verify_coverage(data)
        return

    # Load manual map
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    titre_map = load_titre_map()

    mode = "DRY RUN" if args.dry_run else "APPLY"
    print(f"\n=== Curate ALL Titres ({mode}) ===")
    print(f"Manual map entries: {len(titre_map)}")

    stats, changes = apply_all_titres(data, titre_map, dry_run=args.dry_run)

    if args.dry_run and (args.show_failures or args.show_generated):
        for line in changes:
            if args.show_failures and "FAIL" in line:
                print(line)
            elif args.show_generated and "GEN" in line:
                print(line)

    print(f"\n--- Summary ---")
    print(f"Manual:           {stats['manual']}")
    print(f"Generated:        {stats['generated']}")
    print(f"Skipped (exists): {stats['skipped_existing']}")
    print(f"Failed:           {stats['failed']}")

    if not args.dry_run and (stats["manual"] + stats["generated"]) > 0:
        save_data(data)
        print(f"\nSaved to {DATA_FILE}")
        verify_coverage(data)
    elif args.dry_run:
        print(f"\nDry run - no changes written.")


if __name__ == "__main__":
    main()
