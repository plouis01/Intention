"""CGI article reference extraction from law text.

This module extracts references to CGI (Code Général des Impôts) articles
from the text of finance law articles using regex patterns.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Iterator

logger = logging.getLogger(__name__)


@dataclass
class CGIReference:
    """A reference to a CGI article found in law text."""

    article_ref: str  # Normalized reference: "150-0 B ter", "787 B", etc.
    raw_match: str  # Original text that was matched
    context: str  # Surrounding text for context
    modification_type: str  # "modifie", "abroge", "cree", "complete", etc.
    reference_type: str = "direct"  # "direct" or "cross_reference"
    match_start: int = 0  # Start position of match in original text
    match_end: int = 0  # End position of match in original text


@dataclass
class ExtractedModification:
    """A CGI article modification extracted from a law."""

    cgi_article: str  # "150-0 B ter"
    law_numero: str  # "2023-1322"
    law_type: str  # "LF", "LFR", "LFSS"
    law_year: int
    article_in_law: str  # "Art. 24"
    date_promulgation: str  # "2023-12-29"
    nature: str  # "Modification", "Création", "Abrogation"
    context: str  # Text excerpt showing the modification
    confidence: float = 1.0  # Confidence score (1.0 = high, 0.5 = medium, 0.0 = low)


class CGIExtractor:
    """Extract CGI article references from law text."""

    # Latin ordinals used in CGI article numbering (longest first for regex matching)
    ORDINALS = r"(?:quaterdecies|quindecies|sexdecies|terdecies|undecies|duodecies|quinquies|septies|octies|nonies|decies|sexies|quater|bis|ter)"

    # Article suffix patterns:
    # - Letter + ordinal: "B ter", "A bis"
    # - Ordinal + letter: "quater B" (like 244 quater B)
    # - Just letter: "A", "B"
    # - Just ordinal: "bis", "ter" (like 39 bis)
    # - Number-letter combo: "-0 A", "-0 B ter"
    # Note: ordinals ordered longest-first for correct regex matching
    ARTICLE_SUFFIX = (
        r"(?:"
        r"(?:\s*[-–]\s*\d+)?\s*[A-Z](?:\s+(?:quaterdecies|quindecies|sexdecies|terdecies|undecies|duodecies|quinquies|septies|octies|nonies|decies|sexies|quater|bis|ter))?"  # -0 B ter
        r"|(?:\s+(?:quaterdecies|quindecies|sexdecies|terdecies|undecies|duodecies|quinquies|septies|octies|nonies|decies|sexies|quater|bis|ter))(?:\s+[A-Z])?"  # quater B or just bis
        r")?"
    )

    # Ordinals pattern - longest first for correct regex matching
    _ORD = r"(?:quaterdecies|quindecies|sexdecies|terdecies|undecies|duodecies|quinquies|septies|octies|nonies|decies|sexies|quater|bis|ter)"

    # Patterns for CGI article references
    # These match various ways articles are referenced in French legal text
    # Note: All patterns use _ORD which has ordinals ordered longest-first
    #
    # Article number pattern - captures formats like:
    #   - Simple: "150", "787"
    #   - With dash: "150-0", "238-0"
    #   - With letter(s): "150-0 B", "787 B", "150 VH"
    #   - With ordinal: "150-0 B ter", "244 quater B", "150 VH bis"
    # Note: use (?-i:[A-Z]) to ensure uppercase-only matching even when
    # patterns are compiled with re.IGNORECASE (which is needed for ordinals)
    #
    # _CAP captures these article number formats:
    #   150, 150-0, 150-0 B, 150-0 B ter, 150 VH, 150 VH bis,  (dash-first)
    #   244 quater, 244 quater B,                                 (ordinal+letter)
    #   199 terdecies-0 A, 199 terdecies-0 AB,                   (ordinal+dash)
    #   199 terdecies-0 A bis, 1649 quater-0 B bis               (ordinal+dash+letter+ordinal)
    _CAP = (
        r"(\d+"                                                         # base number
        r"(?:\s*[-–]\s*\d+)?"                                          # optional: dash-number
        r"(?:\s*(?-i:[A-Z]){1,2}(?:\s+{_ORD})?)?"                     # optional: letter(s) [ordinal]
        r"(?:\s+{_ORD}"                                                 # optional: ordinal...
        r"(?:"                                                          #   optional continuation:
        r"\s*[-–]\s*\d+(?:\s+(?-i:[A-Z]){1,2})?(?:\s+{_ORD})?"        #     dash-number [letter(s)] [ordinal]
        r"|"                                                            #   OR
        r"(?:\s+(?-i:[A-Z]){1,2})"                                     #     just letter(s)
        r")?"                                                           #   end continuation
        r")?)"                                                          # end ordinal group + end capture
    ).replace("{_ORD}", _ORD)
    _ART_NUM = _CAP
    # Non-capturing variant for use in prefixes (e.g. "après l'article X, ...")
    _NC = _CAP.replace("(", "(?:", 1).rstrip(")") + ")"

    CGI_PATTERNS = [
        # "l'article 150-0 B ter du code général des impôts" or "l'article 244 quater B du CGI"
        rf"(?:l[''])?article\s+{_CAP}\s+du\s+(?:code\s+général\s+des\s+impôts|CGI)",
        # "articles 150-0 B ter et 150-0 D du CGI"
        rf"articles?\s+{_CAP}\s+du\s+(?:code\s+général\s+des\s+impôts|CGI)",
        # "l'article 150-0 B ter" (alone, near "modifié", "complété", etc.)
        rf"(?:l[''])?article\s+{_CAP}\s+(?:est|sont)\s+(?:ainsi\s+)?(?:modifi|compl[eé]t|abrog|r[eé]dig)",
        # "au I de l'article 150-0 B ter"
        rf"(?:au|aux|du|le|la)\s+(?:[IVX]+\s+(?:et\s+[IVX]+\s+)?(?:de\s+)?)?(?:l[''])?article\s+{_CAP}",
        # Just "150-0 B ter" or "244 quater B" in context of CGI modifications
        rf"(\d{{2,4}}(?:\s*[-–]\s*\d+)?(?:\s*(?-i:[A-Z]){{1,2}}(?:\s+{_ORD})?)?(?:\s+{_ORD}(?:\s+(?-i:[A-Z]))?)?)\s+du\s+(?:même\s+)?code",
        # Specific pattern for "244 quater B" style (number + ordinal + letter)
        # Use negative lookahead to ensure letter is not followed by more letters (avoid matching "d" from "du")
        rf"(?:l[''])?article\s+(\d+\s+{_ORD}(?:\s+(?-i:[A-Z])(?![a-zA-Z]))?)",
        # Pattern for articles like "39 duodecies" or "151 septies A"
        rf"(?:l[''])?article\s+(\d+\s+{_ORD}(?:\s+(?-i:[A-Z]))?)\s+(?:du|est|sont)",
        # Pattern for "199 terdecies-0 A" / "199 terdecies-0 AB" style (number + ordinal + dash + number + letter(s))
        rf"(?:l[''])?article\s+(\d+\s+{_ORD}(?:\s*-\s*\d+)?(?:\s+(?-i:[A-Z]){{1,2}})?)\s+du\s+(?:code\s+général\s+des\s+impôts|CGI)",
        # Pattern for "1649 quater-0 B bis" style (number + ordinal + dash + number + letter(s) + ordinal)
        rf"(?:l[''])?article\s+(\d+\s+{_ORD}(?:\s*-\s*\d+)?(?:\s+(?-i:[A-Z]){{1,2}})?\s+{_ORD})\s+du\s+(?:code\s+général\s+des\s+impôts|CGI)",
        # Pattern for multi-letter suffixes like "150 VH bis" (number + 2 letters + ordinal)
        rf"(?:l[''])?article\s+(\d+\s+(?-i:[A-Z]){{1,2}}(?:\s+{_ORD})?)\s+du\s+(?:code\s+général\s+des\s+impôts|CGI)",

        # === NEW PATTERNS for better coverage ===

        # "un article 150-0 B ter ainsi rédigé" (article creation)
        rf"un\s+article\s+{_CAP}\s+ainsi\s+r[eé]dig[eé]",
        # "il est inséré un article 150-0 B ter" (article insertion)
        rf"(?:il\s+est\s+)?(?:ins[eé]r[eé]|ajout[eé]|cr[eé][eé])\s+un\s+article\s+{_CAP}",
        # "après l'article 150-0 B bis, il est inséré un article 150-0 B ter"
        rf"apr[eè]s\s+l['']article\s+{_NC},?\s+(?:il\s+est\s+)?ins[eé]r[eé]\s+un\s+article\s+{_CAP}",
        # "de l'article 150-0 B ter" (reference in context)
        rf"de\s+l['']article\s+{_CAP}",
        # "à l'article 150-0 B ter" (reference in context)
        rf"[àa]\s+l['']article\s+{_CAP}",
        # "articles 150-0 B ou 150-0 B ter" - capture each article in "ou" lists
        rf"articles?\s+{_NC}\s+ou\s+{_CAP}",
        # "articles 150-0 B et 150-0 B ter" - capture each article in "et" lists
        rf"articles?\s+{_NC}\s+et\s+{_CAP}",
        # "« Art. 150-0 B ter" (article definition in legal text)
        rf"[«\"]\s*Art\.\s*{_CAP}",
        # "l'article 150-0 B ter intervenue" or "intervenu" (past participle reference)
        rf"(?:l[''])?article\s+{_CAP}\s+intervenu",
        # Generic "l'article X" followed by common words (broader catch)
        rf"(?:l[''])?article\s+{_CAP}(?:\s+(?:du|est|sont|pr[eé]vu|mentionn[eé]|vis[eé]|d[eé]fini|applicable))",
        # "l'article X," followed by comma (article in enumeration or apposition)
        rf"(?:l[''])?article\s+{_CAP}\s*,",
    ]

    # Patterns to detect modification type
    MODIFICATION_PATTERNS = {
        "Création": [
            r"est\s+(?:ainsi\s+)?(?:créé|inséré|ajouté)",
            r"(?:il\s+est\s+)?(?:créé|inséré|ajouté)\s+un\s+article",
            r"après\s+l['']article.*est\s+inséré",
        ],
        "Modification": [
            r"est\s+(?:ainsi\s+)?modifié",
            r"est\s+(?:ainsi\s+)?complété",
            r"est\s+(?:ainsi\s+)?rédigé",
            r"sont\s+(?:ainsi\s+)?modifiés",
            r"les\s+mots.*sont\s+(?:remplacés|supprimés)",
            r"(?:au|le)\s+[IVX]+.*(?:est|sont)\s+(?:modifié|complété|supprimé)",
        ],
        "Abrogation": [
            r"est\s+(?:ainsi\s+)?abrogé",
            r"sont\s+(?:ainsi\s+)?abrogés",
            r"(?:est|sont)\s+supprimé",
        ],
    }

    # Known CGI articles for validation and confidence scoring.
    # Comprehensive list of frequently-modified articles across all domains.
    #
    # Note on articles that don't appear in article_modifications.json:
    # Some articles listed here are never directly modified by finance laws
    # 2012-2025 in our database. They are kept for confidence scoring in case
    # they appear in debates or future law imports. Two categories:
    #
    # (a) Not referenced in any law article text between 2012 and 2025:
    #     1 A, 44 quinquies, 151 septies B, 168, 294, 750, 750 ter,
    #     1635 ter, 1649-0 A, 1649 quater-0 B bis
    #
    # (b) Only appear as suffixed variants (parent number never used bare) between 2012 and 2025:
    #     217 (only 217 undecies), 1384 (only 1384 A/B/C/E/F),
    #     1388 (only 1388 ter/sexies/octies), 1466 (only 1466 A/D/E/F),
    #     1635 (only 1635 bis/sexies/etc.), 1679 (only 1679 septies),
    #     1681 (only 1681 ter/sexies), 1691 (only 1691 ter)
    #
    # The suffixed variants ARE in the index - no data is missing.
    # This will need to be checked when coverage is enlarged to the 1950's
    KNOWN_CGI_ARTICLES = {
        # IR - Impôt sur le revenu (basics)
        "1", "1 A", "2", "4 B", "5", "6", "13", "14", "93",
        # BIC/BNC/BA - Revenus professionnels
        "34", "35", "38", "39", "39 bis", "39 duodecies",
        "44 sexies", "44 sexies A", "44 octies", "44 octies A",
        "44 quaterdecies", "44 quinquies", "44 sexdecies",
        # Traitements et salaires
        "62", "72", "73", "80", "80 duodecies", "80 quaterdecies", "81", "83",
        # Plus-values
        "150-0 A", "150-0 B", "150-0 B bis", "150-0 B ter", "150-0 D", "150-0 D ter",
        "150 U", "150 VH bis", "151 septies", "151 septies A", "151 septies B",
        # Déductions, réductions, crédits d'impôt
        "119 bis", "145", "154 bis", "154 bis-0 A", "154 quinquies",
        "156", "157", "158", "163 bis G", "163 quinquies B", "167 bis", "168",
        "182 B", "199 terdecies-0 A", "199 terdecies-0 AB",
        "200", "200 A", "200 quater", "200 quater A", "200 quater B",
        # IS - Impôt sur les sociétés
        "205", "206", "207", "209", "209 B", "210 A", "210 B", "210 F",
        "211", "212", "212 bis", "217", "219", "220 quinquies",
        "223 A", "223 B", "223 C",
        # Crédit d'impôt recherche
        "244 quater B", "244 quater C",
        # International
        "238 A", "238 bis", "238 bis-0 A",
        # TVA
        "256", "257", "258", "259", "259 A", "259 B", "261",
        "271", "278", "278-0 bis", "278 sexies", "278 sexies A",
        "279", "279-0 bis", "283", "293 B", "294", "295", "296",
        # Droits d'enregistrement / donations
        "575", "575 A", "726", "750", "750 ter",
        "757 B", "777", "779", "784", "787 B", "787 C",
        "788", "790 A", "790 B", "790 G", "793", "795", "796",
        # ISF/IFI
        "885 I bis", "964", "965", "966", "970", "972", "973",
        "975", "976", "978", "979", "980", "982", "983",
        # Taxes sur les assurances
        "990 I", "990 I bis", "998",
        # Impôts locaux
        "1382", "1383", "1384", "1388", "1395",
        "1407", "1408", "1414", "1417",
        "1447", "1447-0", "1465", "1466",
        "1518", "1519", "1586", "1599", "1600", "1635", "1635 ter",
        # Procédures / pénalités
        "1649", "1649-0 A", "1649 quater-0 B bis", "1649 quater B quater",
        "1679", "1681", "1691",
        "1727", "1728", "1729", "1731",
        "1740 A", "1741", "1743", "1754",
    }

    def __init__(self):
        self._compiled_patterns = [re.compile(p, re.IGNORECASE) for p in self.CGI_PATTERNS]
        self._mod_patterns = {
            nature: [re.compile(p, re.IGNORECASE) for p in patterns]
            for nature, patterns in self.MODIFICATION_PATTERNS.items()
        }

    def normalize_article_ref(self, ref: str) -> str:
        """
        Normalize a CGI article reference to a standard format.

        Examples:
            "150 - 0 B ter" -> "150-0 B ter"
            "150–0 b TER" -> "150-0 B ter"
            "199terdecies-0A" -> "199 terdecies-0 A"
            "244 QUATER B" -> "244 quater B"
            "244quaterB" -> "244 quater B"
        """
        # Remove extra whitespace
        ref = " ".join(ref.split())

        # Normalize dashes (en-dash, em-dash -> hyphen)
        ref = ref.replace("–", "-").replace("-", "-")

        # Normalize spacing around hyphens in article numbers
        # "150 - 0" -> "150-0", but keep "terdecies-0" as is
        ref = re.sub(r"(\d)\s*-\s*(\d)", r"\1-\2", ref)

        # Ensure space before letter suffix if missing
        # "150-0B" -> "150-0 B"
        ref = re.sub(r"(\d)([A-Z])", r"\1 \2", ref)

        # Ensure space between number and ordinal if missing (longest ordinals first)
        # "244quater" -> "244 quater", "199terdecies" -> "199 terdecies"
        ordinals_list = ["quaterdecies", "quindecies", "sexdecies", "terdecies",
                         "undecies", "duodecies", "quinquies", "septies", "octies",
                         "nonies", "decies", "sexies", "quater", "bis", "ter"]
        # Use non-capturing group (?:...) for alternation to avoid nested group issues
        ordinals_nc = r"(?:" + "|".join(ordinals_list) + r")"
        ref = re.sub(rf"(\d)({ordinals_nc})", r"\1 \2", ref, flags=re.IGNORECASE)

        # Ensure space between ordinal and letter if missing
        # "quaterB" -> "quater B" - but NOT "terdecies" -> "ter decies"
        # Only match if followed by a single uppercase letter at word boundary
        ref = re.sub(rf"({ordinals_nc})([A-Z])(?![a-z])", r"\1 \2", ref, flags=re.IGNORECASE)

        # Normalize latin ordinals to lowercase (longest first to avoid partial matches)
        for ordinal in ["quaterdecies", "quindecies", "sexdecies", "terdecies",
                        "undecies", "duodecies", "quinquies", "septies", "octies",
                        "nonies", "decies", "sexies", "quater", "bis", "ter"]:
            ref = re.sub(rf"\b{ordinal}\b", ordinal, ref, flags=re.IGNORECASE)

        # Uppercase single letters (A, B, C) - but not ordinals
        ref = re.sub(r"\b([a-z])\b", lambda m: m.group(1).upper(), ref)

        # Clean up any double spaces
        ref = " ".join(ref.split())

        return ref.strip()

    def extract_cgi_references(self, text: str, context_chars: int = 200) -> list[CGIReference]:
        """
        Extract all CGI article references from text.

        Args:
            text: Law article text to search
            context_chars: Number of characters of context to include

        Returns:
            List of CGIReference objects
        """
        # Collect all matches with position info
        all_matches: list[tuple[int, int, str, str]] = []  # (start, end, raw, normalized)

        for pattern in self._compiled_patterns:
            for match in pattern.finditer(text):
                raw_match = match.group(1)
                normalized = self.normalize_article_ref(raw_match)
                # Use the position of the captured group
                match_start = match.start(1)
                match_end = match.end(1)
                all_matches.append((match_start, match_end, raw_match, normalized))

        # Extract individual articles from comma/et-separated lists
        all_matches.extend(self._extract_from_article_lists(text))

        # Sort by start position, then by length (longest first)
        all_matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))

        # Filter overlapping matches: keep only the longest match at each position
        # and skip matches that are substrings of longer ones
        filtered_matches: list[tuple[int, int, str, str]] = []
        for match in all_matches:
            match_start, match_end, raw, normalized = match
            # Check if this match is contained within or overlaps with an existing longer match
            is_redundant = False
            for existing in filtered_matches:
                ex_start, ex_end, ex_raw, ex_normalized = existing
                # Skip if this normalized ref is a prefix of an already-found ref
                if ex_normalized.startswith(normalized) and ex_normalized != normalized:
                    is_redundant = True
                    break
                # Skip if this match overlaps with existing and existing is longer
                if match_start >= ex_start and match_end <= ex_end:
                    is_redundant = True
                    break
            if not is_redundant:
                filtered_matches.append(match)

        # Build references from filtered matches (deduplicate by normalized ref)
        references = []
        seen_refs = set()

        for match_start, match_end, raw_match, normalized in filtered_matches:
            if normalized in seen_refs:
                continue

            # Détecter les captures accidentelles (ex: "article 2 de" → "2 d")
            if self._is_accidental_capture(text, match_end, raw_match):
                continue

            # Extract context
            start = max(0, match_start - context_chars // 2)
            end = min(len(text), match_end + context_chars // 2)
            context = text[start:end].strip()

            # Rejeter les références à d'autres codes (L. 312-1, code du travail, etc.)
            if self._is_other_code_reference(context, raw_match):
                continue

            seen_refs.add(normalized)

            # Detect modification type from context
            mod_type = self._detect_modification_type(context, normalized)

            references.append(CGIReference(
                article_ref=normalized,
                raw_match=raw_match,
                context=context,
                modification_type=mod_type,
                match_start=match_start,
                match_end=match_end,
            ))

        return references

    def _detect_modification_type(self, context: str, article_ref: str = "") -> str:
        """Detect the type of modification from context text.

        When article_ref is provided, first tries article-specific patterns
        that verify the modification verb is syntactically attached to this
        particular article (not a different one mentioned in the same context).
        """
        if article_ref:
            escaped = re.escape(article_ref)
            # Article-specific patterns: verb attached to *this* article
            article_patterns = {
                "Création": [
                    rf"article\s+{escaped}.*?est\s+(?:ainsi\s+)?(?:créé|inséré|ajouté)",
                    rf"(?:inséré|créé|ajouté)\s+un\s+article\s+{escaped}",
                ],
                "Modification": [
                    rf"article\s+{escaped}.*?est\s+(?:ainsi\s+)?(?:modifié|complété|rédigé)",
                ],
                "Abrogation": [
                    rf"article\s+{escaped}.*?est\s+(?:ainsi\s+)?(?:abrogé|supprimé)",
                ],
            }
            for nature, patterns in article_patterns.items():
                for pattern in patterns:
                    if re.search(pattern, context, re.IGNORECASE):
                        return nature

        # Fall through to generic patterns (existing behavior)
        for nature, patterns in self._mod_patterns.items():
            for pattern in patterns:
                if pattern.search(context):
                    return nature
        return "Modification"  # Default

    def _is_accidental_capture(self, text: str, match_end: int, raw_match: str) -> bool:
        """
        Détecte si la capture est accidentelle (lettre = début du mot suivant).

        Exemple: "article 2 de la loi" capture "2 d" accidentellement.
        On vérifie si après le match, le texte continue avec "e " ou "u " (de/du).
        Exception: "article 1 du CGI" / "du code général" / "du même code" are
        legitimate CGI references where the captured "d" is from "du".
        """
        # Vérifier ce qui suit immédiatement le match
        after_match = text[match_end:match_end + 10].lower()

        # Pattern: lettre capturée + suite du mot "de" ou "du"
        # "2 d" suivi de "e " ou "u " = capture accidentelle de "de" ou "du"
        if re.match(r'^e\s', after_match) or re.match(r'^u\s', after_match):
            # Vérifier que le match se termine par une lettre seule
            if re.search(r'\s[a-zA-Z]$', raw_match):
                # Exception: "du CGI", "du code général", "du même code"
                # are legitimate CGI references, not accidental captures
                after_full = text[match_end:match_end + 40].lower()
                if re.match(r'^[eu]\s+(cgi|code\s+général|même\s+code)', after_full):
                    return False
                return True

        # Pattern: "1e" suivi de "r" = capture partielle de "1er"
        if raw_match.lower().endswith('1e') and after_match.startswith('r'):
            return True

        # Pattern: capture partielle d'ordinal latin
        # "278 t" suivi de "er" = capture partielle de "278 ter"
        # "244 q" suivi de "uater" = capture partielle de "244 quater"
        ordinal_continuations = {
            't': 'er',      # ter
            'q': 'uater',   # quater
            'b': 'is',      # bis
            's': 'exies',   # sexies, septies
            'o': 'cties',   # octies
            'n': 'onies',   # nonies
            'd': 'ecies',   # decies
            'u': 'ndecies', # undecies
        }
        if re.search(r'\s[a-zA-Z]$', raw_match):
            last_letter = raw_match[-1].lower()
            if last_letter in ordinal_continuations:
                expected = ordinal_continuations[last_letter]
                if after_match.startswith(expected):
                    return True

        return False

    def _is_other_code_reference(self, context: str, raw_match: str) -> bool:
        """
        Détecte si la référence d'article pointe vers un autre code juridique.

        Uses a narrow window around the matched article to avoid rejecting
        CGI articles that merely appear near references to other codes in
        the same paragraph.
        """
        match_pos = context.find(raw_match)

        # Check if the matched article is prefixed with L./D./R.
        # (meaning it's a reference TO another code, e.g. "L. 111-1")
        # Also check if the match is embedded in a larger L./D./R. reference
        # (e.g. "28-3" inside "L. 5211-28-3")
        if match_pos > 0:
            before = context[max(0, match_pos - 15):match_pos]
            if re.search(r'[LDR]\.\s*\S*$', before):
                return True

        # Check for explicit other code names in a narrow window (±60 chars)
        # around the match, but only within the same clause (no sentence
        # boundary between the other code and the article match)
        if match_pos >= 0:
            narrow_start = max(0, match_pos - 60)
            narrow_end = min(len(context), match_pos + len(raw_match) + 60)
        else:
            narrow_start = 0
            narrow_end = len(context)
        narrow_context = context[narrow_start:narrow_end].lower()

        other_codes = [
            "code du travail", "code de l'action sociale",
            "code de la sécurité sociale", "code de commerce",
            "code civil", "code pénal", "code de l'urbanisme",
            "code de la construction", "code monétaire",
            "ordonnance n°", "ordonnance no",
        ]

        # Position of article match within the narrow context
        match_in_narrow = match_pos - narrow_start if match_pos >= 0 else 0

        for code in other_codes:
            code_pos = narrow_context.find(code)
            if code_pos < 0:
                continue
            # Check if a sentence boundary (;  .) separates the code from the match
            if code_pos < match_in_narrow:
                between = narrow_context[code_pos + len(code):match_in_narrow]
            else:
                between = narrow_context[match_in_narrow:code_pos]
            if re.search(r'[;.]', between):
                continue  # Different clause, don't reject
            return True

        return False

    def _extract_from_article_lists(self, text: str) -> list[tuple[int, int, str, str]]:
        """
        Extract individual articles from comma/et-separated article lists.

        Handles patterns like:
        - "articles 575 et 575 A du code général des impôts"
        - "articles 790 B, 790 D, 790 E et 790 F est supprimé"
        - "les articles 1388 ter, 1388 sexies, 1388 octies"
        """
        matches = []

        # Build list pattern: "articles X, Y, Z et W" or "articles X, Y, Z"
        # Note: do NOT use re.IGNORECASE - it would make [A-Z]{1,2} match
        # "et" and "du", consuming list separators
        nc = self._NC
        list_pattern = re.compile(
            r"(?:(?:[Ll]es|[Dd]es|[Aa]ux)\s+)?[Aa]rticles\s+"
            + r"(" + nc + r"(?:\s*,\s*" + nc + r")*"
            + r"(?:\s+et\s+" + nc + r")?)",
        )

        item_pattern = re.compile(self._CAP)

        for list_match in list_pattern.finditer(text):
            list_str = list_match.group(1)
            # Must have at least 2 items (comma or "et") to be a list
            if "," not in list_str and " et " not in list_str.lower():
                continue

            list_start = list_match.start(1)

            for item_match in item_pattern.finditer(list_str):
                raw = item_match.group(1)
                normalized = self.normalize_article_ref(raw)
                abs_start = list_start + item_match.start(1)
                abs_end = list_start + item_match.end(1)
                matches.append((abs_start, abs_end, raw, normalized))

        return matches

    def _classify_references(self, text: str, refs: list[CGIReference]) -> list[CGIReference]:
        """Classify references as direct modifications or cross-references.

        Classification rules (applied in priority order):
        1. Guillemet rule: refs inside « » are cross-references (quoted text)
        2. Cross-reference indicators: "mentionné à", "prévu à", etc.
        3. Chapeau/primary target: refs with modification verbs attached are direct
        4. Default: keep as "direct" (conservative)
        """
        # Cross-reference indicator phrases (preceding the reference)
        cross_ref_indicators = [
            "mentionné à", "mentionnée à", "mentionnés à", "mentionnées à",
            "prévu à", "prévue à", "prévus à", "prévues à",
            "défini à", "définie à", "définis à", "définies à",
            "visé à", "visée à", "visés à", "visées à",
            "au sens de",
            "dans les conditions prévues à",
            "en application de",
            "dans les conditions fixées à",
            "dans les conditions définies à",
        ]

        for ref in refs:
            pos = ref.match_start

            # Rule 1: Guillemet rule - count « and » before match position
            text_before = text[:pos]
            opens = text_before.count("«")
            closes = text_before.count("»")
            if opens > closes:
                ref.reference_type = "cross_reference"
                continue

            # Rule 2: Cross-reference indicator phrases before the match
            # Look in a window of ~80 chars before the match
            window_start = max(0, pos - 80)
            before_window = text[window_start:pos].lower()
            is_cross_ref = False
            for indicator in cross_ref_indicators:
                if indicator in before_window:
                    is_cross_ref = True
                    break
            if is_cross_ref:
                ref.reference_type = "cross_reference"
                continue

            # Rule 3: Chapeau/primary target - modification verb attached
            # Look in a window after the match for modification verbs
            after_start = ref.match_end
            after_window = text[after_start:after_start + 80].lower()
            mod_verbs = [
                "est ainsi modifié", "est modifié", "est ainsi complété",
                "est complété", "est ainsi rédigé", "est rédigé",
                "est ainsi créé", "est créé", "est inséré",
                "est abrogé", "est supprimé",
                "sont ainsi modifiés", "sont modifiés",
                "sont abrogés", "sont supprimés",
            ]
            for verb in mod_verbs:
                if verb in after_window:
                    ref.reference_type = "direct"
                    break

            # Rule 4: Default - keep as "direct" (conservative)

        return refs

    def extract_modifications_from_law(
        self,
        law_articles: list[dict],
        law_info: dict,
    ) -> list[ExtractedModification]:
        """
        Extract all CGI modifications from a law's articles.

        Args:
            law_articles: List of article dicts with 'numero_article' and 'contenu_texte'
            law_info: Dict with 'numero', 'type', 'annee_budgetaire', 'date_promulgation'

        Returns:
            List of ExtractedModification objects
        """
        modifications = []

        for article in law_articles:
            article_num = article.get("numero_article", "")
            text = article.get("contenu_texte", "")

            if not text:
                continue

            # Extract CGI references from this article
            refs = self.extract_cgi_references(text)
            refs = self._classify_references(text, refs)

            for ref in refs:
                # Calculate confidence based on various factors
                confidence = self._calculate_confidence(ref)

                # Cross-references score below min_confidence threshold
                if ref.reference_type == "cross_reference":
                    confidence = min(confidence, 0.2)

                modifications.append(ExtractedModification(
                    cgi_article=ref.article_ref,
                    law_numero=law_info.get("numero", ""),
                    law_type=law_info.get("type", ""),
                    law_year=law_info.get("annee_budgetaire", 0),
                    article_in_law=f"Art. {article_num}",
                    date_promulgation=law_info.get("date_promulgation", ""),
                    nature=ref.modification_type,
                    context=ref.context,
                    confidence=confidence,
                ))

        return modifications

    def _calculate_confidence(self, ref: CGIReference) -> float:
        """
        Calculate confidence score for a CGI reference.

        Returns:
            Float between 0.0 and 1.0
        """
        confidence = 0.5  # Base confidence

        # Higher confidence if it's a known CGI article
        if ref.article_ref in self.KNOWN_CGI_ARTICLES:
            confidence += 0.3

        # Higher confidence if "CGI" or "code général des impôts" is in context
        if "CGI" in ref.context or "code général des impôts" in ref.context.lower():
            confidence += 0.2

        # Higher confidence if modification type was clearly detected
        if ref.modification_type != "Modification":
            confidence += 0.1

        return min(1.0, confidence)

    def validate_cgi_article(self, article_ref: str) -> bool:
        """Check if an article reference looks like a valid CGI article."""
        normalized = self.normalize_article_ref(article_ref)

        # Must start with a number
        if not re.match(r"^\d", normalized):
            return False

        # Must be reasonable length
        if len(normalized) < 1 or len(normalized) > 30:
            return False

        # Should not contain unusual characters
        if re.search(r"[^\d\s\-A-Za-z]", normalized):
            return False

        return True

    def add_known_article(self, article_ref: str) -> None:
        """Add an article to the known CGI articles set."""
        normalized = self.normalize_article_ref(article_ref)
        self.KNOWN_CGI_ARTICLES.add(normalized)


def extract_cgi_modifications_from_db(
    db_session,
    min_confidence: float = 0.5,
) -> dict[str, list[ExtractedModification]]:
    """
    Extract all CGI modifications from the database.

    Args:
        db_session: SQLAlchemy session
        min_confidence: Minimum confidence score to include

    Returns:
        Dict mapping CGI article refs to lists of modifications
    """
    from src.db.models import Article, Loi

    extractor = CGIExtractor()
    modifications_by_article: dict[str, list[ExtractedModification]] = {}

    # Query all laws with their articles
    laws = db_session.query(Loi).all()

    for law in laws:
        law_info = {
            "numero": law.numero,
            "type": law.type,
            "annee_budgetaire": law.annee,
            "date_promulgation": law.date_promulgation.strftime("%Y-%m-%d") if law.date_promulgation else "",
        }

        # Get articles for this law
        articles = db_session.query(Article).filter(Article.loi_id == law.id).all()
        article_dicts = [
            {"numero_article": a.numero_article, "contenu_texte": a.contenu_texte}
            for a in articles
        ]

        # Extract modifications
        mods = extractor.extract_modifications_from_law(article_dicts, law_info)

        for mod in mods:
            if mod.confidence >= min_confidence:
                if mod.cgi_article not in modifications_by_article:
                    modifications_by_article[mod.cgi_article] = []
                modifications_by_article[mod.cgi_article].append(mod)

    # Sort modifications by date for each article
    for article, mods in modifications_by_article.items():
        mods.sort(key=lambda m: m.date_promulgation)

    total_count = sum(len(m) for m in modifications_by_article.values())
    logger.info(
        f"Extracted {total_count} references for {len(modifications_by_article)} CGI articles"
    )

    return modifications_by_article
