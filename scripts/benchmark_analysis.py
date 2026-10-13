"""Benchmark script for article analysis - measures speed and quality before/after optimizations.

Usage:
    # Run benchmark and save results
    python scripts/benchmark_analysis.py --output data/benchmark_before.json

    # Compare two benchmark results
    python scripts/benchmark_analysis.py --compare data/benchmark_before.json data/benchmark_after.json
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests


def run_benchmark(
    article_ref: str = "150-0 B ter",
    base_url: str = "http://localhost:8000",
    output_path: str | None = None,
) -> dict:
    """Run a full analysis benchmark via the streaming endpoint."""
    encoded_ref = quote(article_ref, safe="")
    url = f"{base_url}/cgi/{encoded_ref}/analysis/stream?force_refresh=true"

    print(f"Starting benchmark for article: {article_ref}")
    print(f"URL: {url}")
    print(f"Time: {datetime.now().isoformat()}")
    print("-" * 60)

    # Delete cache first to force fresh analysis
    try:
        requests.delete(f"{base_url}/cgi/{encoded_ref}/cache", timeout=5)
        print("Cache cleared.")
    except Exception as e:
        print(f"Cache clear failed (ok if no cache): {e}")

    # Metrics
    start_time = time.time()
    section_times = []
    section_start = None
    sections = []
    events_by_type = {}
    llm_retry_count = 0
    total_sources_an = 0
    total_sources_senat = 0
    confidence_levels = []
    cross_references = []
    consolidation_periods = []
    forward_looking = []
    synthesis_evolution = []
    errors = []
    token_data = {}

    try:
        with requests.get(url, stream=True, timeout=1200) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "unknown")
                events_by_type[event_type] = events_by_type.get(event_type, 0) + 1

                if event_type == "ping":
                    continue

                elapsed = time.time() - start_time

                if event_type == "status":
                    msg = event.get("data", "")
                    print(f"  [{elapsed:6.1f}s] STATUS: {msg}")
                    if "Analyzing" in msg and section_start is not None:
                        section_times.append(time.time() - section_start)
                    if "Analyzing" in msg:
                        section_start = time.time()

                elif event_type == "article_info":
                    data = event["data"]
                    print(f"  [{elapsed:6.1f}s] ARTICLE: {data['reference']} - {data['total_modifications']} modifications, {data['total_sections']} sections")

                elif event_type == "section":
                    if section_start is not None:
                        section_times.append(time.time() - section_start)
                        section_start = None
                    data = event["data"]
                    mod = data.get("modification", {})
                    n_an = len(data.get("sources_an", []))
                    n_senat = len(data.get("sources_senat", []))
                    certitude = data.get("niveau_certitude", "unknown")
                    total_sources_an += n_an
                    total_sources_senat += n_senat
                    confidence_levels.append(certitude)
                    sections.append(data)
                    tok = data.get("token_usage", {})
                    tok_str = f" | in={tok.get('input_tokens', 0):,} out={tok.get('output_tokens', 0):,}" if tok.get("input_tokens") else ""
                    print(f"  [{elapsed:6.1f}s] SECTION {data.get('index', '?')}: {mod.get('law_ref', '?')} - AN:{n_an} Sénat:{n_senat} certitude:{certitude}{tok_str}")

                elif event_type == "section_error":
                    data = event["data"]
                    errors.append(data)
                    print(f"  [{elapsed:6.1f}s] SECTION_ERROR: {data.get('law_ref', '?')} - {data.get('error', '?')}")

                elif event_type == "consolidation":
                    consolidation_periods.append(event["data"])
                    data = event["data"]
                    print(f"  [{elapsed:6.1f}s] CONSOLIDATION: {data.get('start_year', '?')}-{data.get('end_year', '?')}")

                elif event_type == "synthesis":
                    synthesis_evolution = event.get("data", [])
                    print(f"  [{elapsed:6.1f}s] SYNTHESIS received")

                elif event_type == "forward_looking":
                    forward_looking = event.get("data", [])
                    print(f"  [{elapsed:6.1f}s] FORWARD_LOOKING: {len(forward_looking)} items")

                elif event_type == "cross_references":
                    cross_references = event.get("data", [])
                    print(f"  [{elapsed:6.1f}s] CROSS_REFERENCES: {len(cross_references)} detected")

                elif event_type == "token_usage":
                    token_data = event.get("data", {})
                    cost = token_data.get("estimated_cost_usd", 0)
                    print(
                        f"  [{elapsed:6.1f}s] TOKENS: "
                        f"{token_data.get('total_input_tokens', 0):,} in / "
                        f"{token_data.get('total_output_tokens', 0):,} out - "
                        f"${cost:.4f}"
                    )

                elif event_type == "done":
                    meta = event.get("data", {})
                    print(f"  [{elapsed:6.1f}s] DONE - cached={meta.get('cached', '?')}")

                elif event_type == "error":
                    errors.append({"error": event.get("data", "unknown")})
                    print(f"  [{elapsed:6.1f}s] ERROR: {event.get('data', '?')}")

                elif event_type == "queue":
                    data = event.get("data", {})
                    print(f"  [{elapsed:6.1f}s] QUEUE: position {data.get('position', '?')}")

    except requests.exceptions.Timeout:
        errors.append({"error": "Request timed out after 1200s"})
        print("ERROR: Request timed out!")
    except Exception as e:
        errors.append({"error": str(e)})
        print(f"ERROR: {e}")

    total_time = time.time() - start_time

    # Compute quality metrics
    n_sections = len(sections)
    avg_sources_an = total_sources_an / n_sections if n_sections else 0
    avg_sources_senat = total_sources_senat / n_sections if n_sections else 0
    balance_ratio = min(total_sources_an, total_sources_senat) / max(total_sources_an, total_sources_senat) if max(total_sources_an, total_sources_senat) > 0 else 0

    confidence_map = {"élevé": 3, "moyen": 2, "faible": 1}
    avg_confidence = sum(confidence_map.get(c, 0) for c in confidence_levels) / len(confidence_levels) if confidence_levels else 0

    results = {
        "article_ref": article_ref,
        "timestamp": datetime.now().isoformat(),
        "speed": {
            "total_time_s": round(total_time, 1),
            "section_times_s": [round(t, 1) for t in section_times],
            "avg_section_time_s": round(sum(section_times) / len(section_times), 1) if section_times else 0,
            "min_section_time_s": round(min(section_times), 1) if section_times else 0,
            "max_section_time_s": round(max(section_times), 1) if section_times else 0,
            "events_by_type": events_by_type,
        },
        "quality": {
            "n_sections": n_sections,
            "total_sources_an": total_sources_an,
            "total_sources_senat": total_sources_senat,
            "avg_sources_an_per_section": round(avg_sources_an, 2),
            "avg_sources_senat_per_section": round(avg_sources_senat, 2),
            "balance_ratio_an_senat": round(balance_ratio, 2),
            "confidence_levels": confidence_levels,
            "avg_confidence": round(avg_confidence, 2),
            "n_cross_references": len(cross_references),
            "n_consolidation_periods": len(consolidation_periods),
            "n_forward_looking": len(forward_looking),
            "n_errors": len(errors),
        },
        "sections": sections,
        "cross_references": cross_references,
        "consolidation_periods": consolidation_periods,
        "synthesis_evolution": synthesis_evolution,
        "forward_looking": forward_looking,
        "errors": errors,
        "token_usage": token_data,
    }

    # Print summary
    print("\n" + "=" * 60)
    print("RÉSUMÉ DU BENCHMARK")
    print("=" * 60)
    print(f"Temps total          : {total_time:.1f}s ({total_time/60:.1f} min)")
    print(f"Sections analysées   : {n_sections}")
    print(f"Temps moyen/section  : {results['speed']['avg_section_time_s']}s")
    print(f"Sources AN (total)   : {total_sources_an} (moy: {avg_sources_an:.1f}/section)")
    print(f"Sources Sénat (total): {total_sources_senat} (moy: {avg_sources_senat:.1f}/section)")
    print(f"Balance AN/Sénat     : {balance_ratio:.2f} (1.0 = parfait)")
    print(f"Confiance moyenne    : {avg_confidence:.2f}/3.0")
    print(f"Cross-références     : {len(cross_references)}")
    print(f"Consolidation        : {len(consolidation_periods)}")
    print(f"Erreurs              : {len(errors)}")

    if token_data:
        print(f"\n--- Token Usage ---")
        print(f"Tokens (input)       : {token_data.get('total_input_tokens', 0):,}")
        print(f"Tokens (output)      : {token_data.get('total_output_tokens', 0):,}")
        print(f"Cache write          : {token_data.get('total_cache_write_tokens', 0):,}")
        print(f"Cache read           : {token_data.get('total_cache_read_tokens', 0):,}")
        print(f"LLM calls            : {token_data.get('total_calls', 0)}")
        print(f"Coût estimé          : ${token_data.get('estimated_cost_usd', 0):.4f}")
        by_phase = token_data.get("by_phase", {})
        if by_phase:
            print(f"  Par phase:")
            for phase, stats in sorted(by_phase.items()):
                print(f"    {phase:25s}: {stats['calls']} calls, {stats['input_tokens']:,} in, {stats['output_tokens']:,} out")

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\nRésultats sauvegardés dans : {output_path}")

    return results


def compare_benchmarks(before_path: str, after_path: str):
    """Compare two benchmark results side by side."""
    with open(before_path, encoding="utf-8") as f:
        before = json.load(f)
    with open(after_path, encoding="utf-8") as f:
        after = json.load(f)

    print("=" * 70)
    print("COMPARAISON AVANT / APRÈS OPTIMISATIONS")
    print("=" * 70)

    def _delta(label, before_val, after_val, unit="", lower_is_better=True):
        if before_val == 0:
            pct = "N/A"
        else:
            change = ((after_val - before_val) / before_val) * 100
            sign = "+" if change > 0 else ""
            pct = f"{sign}{change:.0f}%"
            if lower_is_better:
                indicator = "✅" if change < 0 else ("⚠️" if change == 0 else "❌")
            else:
                indicator = "✅" if change > 0 else ("⚠️" if change == 0 else "❌")
        print(f"  {label:35s} {str(before_val):>10s}{unit}  →  {str(after_val):>10s}{unit}  ({pct}) {indicator}")

    print("\n📊 VITESSE")
    _delta("Temps total", before["speed"]["total_time_s"], after["speed"]["total_time_s"], "s")
    _delta("Temps moyen/section", before["speed"]["avg_section_time_s"], after["speed"]["avg_section_time_s"], "s")
    _delta("Temps max section", before["speed"]["max_section_time_s"], after["speed"]["max_section_time_s"], "s")

    print("\n📈 QUALITÉ")
    _delta("Sections", before["quality"]["n_sections"], after["quality"]["n_sections"], "", lower_is_better=False)
    _delta("Sources AN (total)", before["quality"]["total_sources_an"], after["quality"]["total_sources_an"], "", lower_is_better=False)
    _delta("Sources Sénat (total)", before["quality"]["total_sources_senat"], after["quality"]["total_sources_senat"], "", lower_is_better=False)
    _delta("Balance AN/Sénat", before["quality"]["balance_ratio_an_senat"], after["quality"]["balance_ratio_an_senat"], "", lower_is_better=False)
    _delta("Confiance moyenne", before["quality"]["avg_confidence"], after["quality"]["avg_confidence"], "/3", lower_is_better=False)
    _delta("Cross-références", before["quality"]["n_cross_references"], after["quality"]["n_cross_references"], "", lower_is_better=False)
    _delta("Consolidation", before["quality"]["n_consolidation_periods"], after["quality"]["n_consolidation_periods"], "", lower_is_better=False)
    _delta("Erreurs", before["quality"]["n_errors"], after["quality"]["n_errors"], "")

    # Token usage comparison (only if both have token data)
    b_tok = before.get("token_usage", {})
    a_tok = after.get("token_usage", {})
    if b_tok or a_tok:
        print("\n💰 TOKENS")
        _delta("Input tokens", b_tok.get("total_input_tokens", 0), a_tok.get("total_input_tokens", 0), "")
        _delta("Output tokens", b_tok.get("total_output_tokens", 0), a_tok.get("total_output_tokens", 0), "")
        _delta("Cache write", b_tok.get("total_cache_write_tokens", 0), a_tok.get("total_cache_write_tokens", 0), "")
        _delta("Cache read", b_tok.get("total_cache_read_tokens", 0), a_tok.get("total_cache_read_tokens", 0), "", lower_is_better=False)
        _delta("LLM calls", b_tok.get("total_calls", 0), a_tok.get("total_calls", 0), "")
        b_cost = b_tok.get("estimated_cost_usd", 0)
        a_cost = a_tok.get("estimated_cost_usd", 0)
        _delta("Coût estimé ($)", round(b_cost, 4), round(a_cost, 4), "")

    print("\n📋 ÉVÉNEMENTS")
    all_types = set(before["speed"]["events_by_type"].keys()) | set(after["speed"]["events_by_type"].keys())
    for t in sorted(all_types):
        b = before["speed"]["events_by_type"].get(t, 0)
        a = after["speed"]["events_by_type"].get(t, 0)
        _delta(f"  {t}", b, a, "")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark article analysis")
    parser.add_argument("--output", "-o", help="Save results to JSON file")
    parser.add_argument("--compare", "-c", nargs=2, metavar=("BEFORE", "AFTER"), help="Compare two benchmark files")
    parser.add_argument("--article", default="150-0 B ter", help="Article to analyze")
    parser.add_argument("--url", default="http://localhost:8000", help="API base URL")
    args = parser.parse_args()

    if args.compare:
        compare_benchmarks(args.compare[0], args.compare[1])
    else:
        run_benchmark(article_ref=args.article, base_url=args.url, output_path=args.output)
