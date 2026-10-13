#!/usr/bin/env python3
"""
CLI script to generate legislative intent analysis for finance laws.

Analyzes all CGI modifications made by a single finance law.

Usage:
    python scripts/analyse_loi.py "LF 2024"
    python scripts/analyse_loi.py "LFR 2020" --no-intent  # Skip LLM analysis
    python scripts/analyse_loi.py --list  # List available laws
    python scripts/analyse_loi.py --numero 2023-1322  # By law number
"""

import argparse
import logging
import re
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.analysis.law_analyzer import LawAnalyzer, list_available_laws
from src.analysis.renderer import render_law_analysis
from src.utils.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def parse_law_reference(ref: str) -> tuple[str, int]:
    """
    Parse a law reference like "LF 2024" or "LFR 2020".

    Returns:
        Tuple of (law_type, year)
    """
    ref = ref.strip().upper()

    # Try to match "LF 2024", "LFR 2020", "LFSS 2023"
    match = re.match(r"(LF|LFR|LFSS)\s*(\d{4})", ref)
    if match:
        return match.group(1), int(match.group(2))

    raise ValueError(f"Invalid law reference: {ref}. Expected format: 'LF 2024', 'LFR 2020', 'LFSS 2023'")


def list_laws():
    """List all available laws."""
    laws = list_available_laws()

    if not laws:
        print("No laws found in reference file.")
        print("Check data/lois_cgi_1958_2026.json")
        return

    # Group by type
    type_names = {
        "LF": "Lois de finances initiales",
        "LFR": "Lois de finances rectificatives",
        "LFSS": "Lois de financement de la sécurité sociale",
        "LOI": "Lois ordinaires",
        "ORDONNANCE": "Ordonnances",
        "DECRET": "Décrets",
    }
    by_type: dict[str, list] = {}
    for loi in laws:
        law_type = loi.get("type", "AUTRE")
        by_type.setdefault(law_type, []).append(loi)

    print(f"\nAvailable Laws ({len(laws)} total)\n")

    # Show finance types first, then others
    type_order = ["LF", "LFR", "LFSS"] + [
        t for t in sorted(by_type) if t not in ("LF", "LFR", "LFSS")
    ]
    for law_type in type_order:
        type_laws = by_type.get(law_type, [])
        if not type_laws:
            continue

        label = type_names.get(law_type, law_type)
        print(f"## {label} ({len(type_laws)})")
        print()

        for loi in type_laws:
            year = loi.get("annee_budgetaire", "")
            numero = loi.get("numero", "")
            titre = loi.get("titre", "")[:50]
            print(f"  {law_type} {year} (n°{numero})")
            print(f"    {titre}")
        print()


def check_prerequisites(analyze_intent: bool = True) -> bool:
    """Check that prerequisites are met."""
    settings = get_settings()

    if analyze_intent:
        # Check LLM provider
        if settings.llm_provider == "groq" and not settings.get_groq_api_keys():
            print("ERROR: GROQ_API_KEY(S) not configured")
            print("Set GROQ_API_KEYS (comma-separated) or GROQ_API_KEY in .env, or use LLM_PROVIDER=anthropic")
            return False
        elif settings.llm_provider == "anthropic" and not settings.anthropic_api_key:
            print("ERROR: ANTHROPIC_API_KEY not configured")
            print("Set ANTHROPIC_API_KEY in .env or use LLM_PROVIDER=groq")
            return False

    return True


def analyze_law(
    law_ref: str | None = None,
    law_numero: str | None = None,
    output_path: Path | None = None,
    analyze_intent: bool = True,
    max_modifications: int | None = None,
):
    """
    Analyze a finance law and generate markdown document.

    Args:
        law_ref: Law reference like "LF 2024"
        law_numero: Law number like "2023-1322"
        output_path: Output file path
        analyze_intent: Whether to analyze legislative intent with LLM
        max_modifications: Limit number of modifications to analyze
    """
    # Check prerequisites
    if analyze_intent and not check_prerequisites(analyze_intent):
        analyze_intent = False
        print("Continuing without LLM intent analysis...\n")

    # Create analyzer
    analyzer = LawAnalyzer()

    try:
        if law_numero:
            print(f"Analyzing law n°{law_numero}...")
            result = analyzer.analyze_law_by_numero(
                law_numero,
                analyze_intent=analyze_intent,
                max_modifications=max_modifications,
            )
        elif law_ref:
            law_type, law_year = parse_law_reference(law_ref)
            print(f"Analyzing {law_type} {law_year}...")
            result = analyzer.analyze_law(
                law_type,
                law_year,
                analyze_intent=analyze_intent,
                max_modifications=max_modifications,
            )
        else:
            print("ERROR: Must provide law reference or numero")
            return False

    except ValueError as e:
        print(f"ERROR: {e}")
        return False
    except Exception as e:
        print(f"ERROR: Analysis failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Render to markdown
    markdown = render_law_analysis(result)

    # Determine output path
    if output_path is None:
        output_dir = project_root / "data" / "analyses" / "by_law"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{result.law_type}_{result.law_year}.md"

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(markdown)

    print(f"\nAnalysis complete!")
    print(f"Output saved to: {output_path}")
    print(f"\nStatistics:")
    print(f"  Law: {result.full_ref}")
    print(f"  Total articles: {result.total_articles}")
    print(f"  CGI modifications found: {result.total_cgi_modifications}")
    print(f"  Key themes: {', '.join(result.key_themes) if result.key_themes else 'None detected'}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Generate legislative intent analysis for finance laws",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "LF 2024"                    # Analyze LF 2024
  %(prog)s "LFR 2020"                   # Analyze LFR 2020
  %(prog)s "LFSS 2023"                  # Analyze LFSS 2023
  %(prog)s --numero 2023-1322           # Analyze by law number
  %(prog)s "LF 2024" --no-intent        # Skip LLM analysis
  %(prog)s "LF 2024" -o rapport.md      # Custom output path
  %(prog)s --list                       # List available laws
        """,
    )

    parser.add_argument(
        "law_ref",
        nargs="?",
        help="Law reference (e.g., 'LF 2024', 'LFR 2020', 'LFSS 2023')",
    )
    parser.add_argument(
        "-n", "--numero",
        help="Law number (e.g., '2023-1322')",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        help="Output file path (default: data/analyses/by_law/<type>_<year>.md)",
    )
    parser.add_argument(
        "--no-intent",
        action="store_true",
        help="Skip LLM intent analysis (faster, less detail)",
    )
    parser.add_argument(
        "-m", "--max-modifications",
        type=int,
        help="Limit number of modifications to analyze",
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="List available laws",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list:
        list_laws()
        return 0

    if not args.law_ref and not args.numero:
        parser.print_help()
        print("\nERROR: Please provide a law reference (e.g., 'LF 2024') or use --list")
        return 1

    success = analyze_law(
        law_ref=args.law_ref,
        law_numero=args.numero,
        output_path=args.output,
        analyze_intent=not args.no_intent,
        max_modifications=args.max_modifications,
    )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
