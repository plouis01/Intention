#!/usr/bin/env python3
"""Check for false positives in extract_article_mentions."""

import logging
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.disable(logging.INFO)

from sqlalchemy import func

from src.db.models import Intervention, get_session
from src.parsers.debates import extract_article_mentions


def main():
    s = get_session()

    sample = (
        s.query(Intervention)
        .filter(Intervention.articles_mentionnes.is_(None))
        .order_by(func.random())
        .limit(5000)
        .all()
    )

    results = []
    for i in sample:
        mentions = extract_article_mentions(i.contenu or "")
        if not mentions:
            continue
        contenu = i.contenu or ""
        contenu_lower = contenu.lower()
        for m in mentions:
            # Find context around 'article' keyword
            for match in re.finditer(r"articles?\s+", contenu_lower):
                after = contenu_lower[match.start() : match.end() + 50]
                if m in after:
                    ctx_start = max(0, match.start() - 30)
                    ctx_end = min(len(contenu), match.end() + 60)
                    context = contenu[ctx_start:ctx_end].replace("\n", " ")
                    results.append((m, context, i.id))
                    break

    print(f"Total mentions trouvées dans 5000 interventions: {len(results)}\n")

    # Show 50 random examples
    print("=== 50 mentions avec contexte ===")
    random.shuffle(results)
    for mention, context, iid in results[:50]:
        print(f"  [{mention:25s}] ...{context}...")

    # Check false positive patterns
    print("\n=== Faux positifs potentiels ===\n")

    # 1. "article X de la loi" - reference to an article of ANOTHER law (not a bill article)
    fp_autre_loi = []
    for m, ctx, iid in results:
        ctx_after_mention = ctx.lower().split(m)[-1] if m in ctx.lower() else ""
        if any(
            x in ctx_after_mention[:40]
            for x in ["de la loi", "du décret", "de l'ordonnance"]
        ):
            fp_autre_loi.append((m, ctx, iid))

    print(f"1. 'article X de la loi/décret': {len(fp_autre_loi)}/{len(results)} ({100*len(fp_autre_loi)/max(len(results),1):.1f}%)")
    for m, ctx, _ in fp_autre_loi[:8]:
        print(f"   [{m:25s}] ...{ctx}...")

    # 2. "article X du code" - CGI or other code references
    fp_code = []
    for m, ctx, iid in results:
        ctx_after = ctx.lower().split(m)[-1] if m in ctx.lower() else ""
        if "du code" in ctx_after[:30] or "du cgi" in ctx_after[:30]:
            fp_code.append((m, ctx, iid))

    print(f"\n2. 'article X du code/CGI': {len(fp_code)}/{len(results)} ({100*len(fp_code)/max(len(results),1):.1f}%)")
    for m, ctx, _ in fp_code[:8]:
        print(f"   [{m:25s}] ...{ctx}...")

    # 3. Very small numbers (1-5) which might be procedural
    fp_small = [(m, ctx, iid) for m, ctx, iid in results if m in ("1", "2", "3", "4", "5")]
    print(f"\n3. Petits numéros (1-5): {len(fp_small)}/{len(results)} ({100*len(fp_small)/max(len(results),1):.1f}%)")
    for m, ctx, _ in fp_small[:8]:
        print(f"   [{m:25s}] ...{ctx}...")

    # 4. "article L." or "article R." - code references with prefix
    fp_lr = []
    for m, ctx, iid in results:
        ctx_lower = ctx.lower()
        if re.search(r"article\s+[lr]\.\s*" + re.escape(m), ctx_lower):
            fp_lr.append((m, ctx, iid))

    print(f"\n4. 'article L./R. X': {len(fp_lr)}/{len(results)} ({100*len(fp_lr)/max(len(results),1):.1f}%)")
    for m, ctx, _ in fp_lr[:8]:
        print(f"   [{m:25s}] ...{ctx}...")

    # 5. Are these really bill articles or CGI article references?
    print("\n=== Résumé ===")
    print(f"Total mentions: {len(results)}")
    all_fp = set()
    for _, _, iid in fp_autre_loi + fp_code + fp_lr:
        all_fp.add(iid)
    print(f"Potentiels faux positifs (loi/code/L.R.): ~{len(fp_autre_loi) + len(fp_code) + len(fp_lr)}/{len(results)}")
    print(f"Taux: ~{100*(len(fp_autre_loi) + len(fp_code) + len(fp_lr))/max(len(results),1):.1f}%")
    print()
    print("NOTE: 'article X du CGI' n'est PAS un faux positif pour notre usage -")
    print("on veut justement savoir quels articles du CGI sont mentionnés dans les débats.")
    print(f"Vrais faux positifs (hors CGI): ~{len(fp_autre_loi) + len(fp_lr)}/{len(results)} ({100*(len(fp_autre_loi) + len(fp_lr))/max(len(results),1):.1f}%)")

    s.close()


if __name__ == "__main__":
    main()
