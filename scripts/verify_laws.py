#!/usr/bin/env python3
"""Verify that all laws in lois_cgi_1958_2026.json actually exist.

Uses the Légifrance PISTE API to verify JORF IDs.
Checks a stratified sample + all manually injected laws.

Checks:
1. JORF ID validity via API consult/jorf
2. Consistency between JORF response and stored metadata
3. Data integrity: dates, types, numeros
4. Laws without JORF: flagged for review
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from src.utils.config import get_settings

INPUT_FILE = PROJECT_ROOT / "data" / "lois_cgi_1958_2026.json"

# Laws that were manually injected (not from API crawl)
MANUALLY_INJECTED = {
    # Old LF with JORF (injected)
    "58-1374", "59-1454", "60-1384", "61-1396", "62-1529", "63-1241",
    "66-935", "68-1172", "69-1161", "71-1061", "73-1150", "74-1129",
    "95-1346",
    # LFSS
    "96-1160", "99-1140", "2000-1257", "2001-1246",
    "2002-1487", "2004-1370", "2006-1640",
    # LFR
    "2020-289", "2020-1473", "2022-1499", "2024-1167",
}


class ApiVerifier:
    """Verify JORF IDs via the Légifrance API."""

    def __init__(self):
        self.settings = get_settings()
        self._token = None
        self._token_expires = None
        self._client = httpx.Client(timeout=30.0)

    def _get_token(self):
        if self._token and self._token_expires and datetime.now() < self._token_expires:
            return self._token
        resp = self._client.post(
            self.settings.piste_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.piste_client_id,
                "client_secret": self.settings.piste_client_secret,
                "scope": "openid",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = datetime.now() + timedelta(
            seconds=data.get("expires_in", 3600) - 300
        )
        return self._token

    def verify_jorf(self, jorf_id: str) -> dict:
        """Verify a JORF ID via the API. Returns response or error info."""
        token = self._get_token()
        url = f"{self.settings.piste_api_base}/consult/jorf"
        try:
            resp = self._client.post(
                url,
                json={"textCid": jorf_id},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return {"ok": True, "data": data}
            else:
                return {"ok": False, "status": resp.status_code, "body": resp.text[:200]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def close(self):
        self._client.close()


def select_verification_sample(laws: list[dict]) -> list[dict]:
    """Select a stratified sample for verification.

    Strategy:
    - ALL manually injected laws (with JORF)
    - ALL finance laws (LF, LFR, LFSS)
    - Random sample from each decade for other types
    """
    sample = []
    seen = set()

    # 1. All manually injected laws with JORF
    for l in laws:
        if l["numero"] in MANUALLY_INJECTED and l.get("jorf"):
            if l["numero"] not in seen:
                sample.append(l)
                seen.add(l["numero"])

    # 2. All finance laws
    for l in laws:
        if l.get("type") in ("LF", "LFR", "LFSS") and l.get("jorf"):
            if l["numero"] not in seen:
                sample.append(l)
                seen.add(l["numero"])

    # 3. Sample from each decade for LOI/ORDONNANCE/DECRET
    from collections import defaultdict
    by_decade = defaultdict(list)
    for l in laws:
        if l.get("jorf") and l["numero"] not in seen:
            dp = l.get("date_promulgation", "")
            if dp and len(dp) >= 4:
                decade = dp[:3] + "0s"
                by_decade[decade].append(l)

    import random
    random.seed(42)
    for decade, decade_laws in sorted(by_decade.items()):
        n = min(5, len(decade_laws))
        for l in random.sample(decade_laws, n):
            sample.append(l)
            seen.add(l["numero"])

    return sample


def main():
    with open(INPUT_FILE) as f:
        data = json.load(f)

    laws = data["lois"]
    with_jorf = [l for l in laws if l.get("jorf")]
    without_jorf = [l for l in laws if not l.get("jorf")]

    print(f"Total laws: {len(laws)}")
    print(f"With JORF: {len(with_jorf)}")
    print(f"Without JORF: {len(without_jorf)}")
    print()

    # === Select sample ===
    # sample = select_verification_sample(laws)
    sample = with_jorf
    print(f"Verification sample: {len(sample)} laws")
    injected_in_sample = [l for l in sample if l["numero"] in MANUALLY_INJECTED]
    finance_in_sample = [l for l in sample if l.get("type") in ("LF", "LFR", "LFSS")]
    print(f"  - Manually injected: {len(injected_in_sample)}")
    print(f"  - Finance laws (LF/LFR/LFSS): {len(finance_in_sample)}")
    print(f"  - Other (decade sample): {len(sample) - len(set(l['numero'] for l in injected_in_sample) | set(l['numero'] for l in finance_in_sample))}")
    print()

    # === Verify via API ===
    print("=" * 60)
    print("Verifying JORF IDs via Légifrance API...")
    print("=" * 60)

    verifier = ApiVerifier()
    results = []
    errors = []

    try:
        for i, law in enumerate(sample):
            result = verifier.verify_jorf(law["jorf"])
            result["numero"] = law["numero"]
            result["jorf"] = law["jorf"]
            result["type"] = law.get("type", "")
            result["injected"] = law["numero"] in MANUALLY_INJECTED
            results.append(result)

            if not result["ok"]:
                errors.append(result)

            if (i + 1) % 20 == 0 or i == len(sample) - 1:
                print(f"  Checked {i + 1}/{len(sample)} - errors: {len(errors)}")

            time.sleep(0.1)  # Rate limiting
    finally:
        verifier.close()

    # === Report ===
    print()
    print("=" * 60)
    print("API VERIFICATION RESULTS")
    print("=" * 60)

    ok_count = sum(1 for r in results if r["ok"])
    print(f"\nValid JORF IDs: {ok_count}/{len(sample)}")

    if errors:
        print(f"\nFailed JORF verifications ({len(errors)}):")
        for e in sorted(errors, key=lambda x: x["numero"]):
            status = e.get("error", f"HTTP {e.get('status', '?')}: {e.get('body', '')[:100]}")
            injected_tag = " [INJECTED]" if e.get("injected") else ""
            print(f"  {e['numero']} ({e['jorf']}) {e['type']}{injected_tag}: {status}")

    # Cross-check metadata for successful ones
    print()
    print("=" * 60)
    print("METADATA CROSS-CHECK (successful verifications)")
    print("=" * 60)

    mismatches = []
    for r in results:
        if not r["ok"]:
            continue
        api_data = r.get("data", {})
        # Try to extract title from API response
        title_from_api = ""
        if "title" in api_data:
            title_from_api = api_data["title"]
        elif "text" in api_data and isinstance(api_data["text"], dict):
            title_from_api = api_data["text"].get("title", "")
        elif "title" in api_data.get("executionResult", {}):
            title_from_api = api_data["executionResult"]["title"]

        # Check if the numero appears in the API title
        numero = r["numero"]
        if title_from_api and numero not in title_from_api:
            # Old format: 59-1472 might appear as n° 59-1472
            short_num = numero.lstrip("0")
            if short_num not in title_from_api:
                mismatches.append({
                    "numero": numero,
                    "jorf": r["jorf"],
                    "api_title": title_from_api[:120],
                })

    if mismatches:
        print(f"\nPotential mismatches ({len(mismatches)}):")
        for m in mismatches[:20]:
            print(f"  {m['numero']} ({m['jorf']})")
            print(f"    API title: {m['api_title']}")
    else:
        print("\nNo title mismatches found in verified laws.")

    # === Laws without JORF ===
    print()
    print("=" * 60)
    print(f"LAWS WITHOUT JORF ID ({len(without_jorf)})")
    print("=" * 60)
    for l in without_jorf:
        print(f"  {l['numero']} | {l['type']} | {l.get('titre', '')[:80]}")

    # === Data integrity checks ===
    print()
    print("=" * 60)
    print("DATA INTEGRITY CHECKS (all 743 laws)")
    print("=" * 60)

    # Check dates
    bad_dates = []
    for l in laws:
        dp = l.get("date_promulgation", "")
        if not dp:
            bad_dates.append((l["numero"], "missing"))
        elif len(dp) < 10:
            bad_dates.append((l["numero"], f"short: {dp}"))
        else:
            try:
                year = int(dp[:4])
                if year < 1958 or year > 2026:
                    bad_dates.append((l["numero"], f"year out of range: {dp}"))
            except ValueError:
                bad_dates.append((l["numero"], f"invalid: {dp}"))

    print(f"\nBad dates: {len(bad_dates)}")
    for num, issue in bad_dates:
        print(f"  {num}: {issue}")

    # Check types
    valid_types = {"LOI", "ORDONNANCE", "DECRET", "LF", "LFR", "LFSS"}
    bad_types = [(l["numero"], l.get("type", "")) for l in laws if l.get("type") not in valid_types]
    print(f"\nBad types: {len(bad_types)}")
    for num, t in bad_types:
        print(f"  {num}: {t}")

    # Check annee_budgetaire
    no_annee = [l for l in laws if "annee_budgetaire" not in l]
    print(f"\nMissing annee_budgetaire: {len(no_annee)}")

    # Check annee_budgetaire consistency
    bad_annee = []
    for l in laws:
        ab = l.get("annee_budgetaire")
        dp = l.get("date_promulgation", "")
        if ab and dp:
            try:
                year = int(dp[:4])
                if abs(ab - year) > 2:
                    bad_annee.append((l["numero"], f"annee={ab} vs date={year}"))
            except ValueError:
                pass
    print(f"\nAnnee_budgetaire far from date: {len(bad_annee)}")
    for num, issue in bad_annee:
        print(f"  {num}: {issue}")

    # Check JORF format
    bad_jorf = []
    for l in with_jorf:
        jorf = l["jorf"]
        if not re.match(r"^JORFTEXT\d{12}$", jorf):
            bad_jorf.append((l["numero"], jorf))
    print(f"\nBad JORF format: {len(bad_jorf)}")
    for num, jorf in bad_jorf:
        print(f"  {num}: {jorf}")

    # Budget year coverage for finance laws
    print()
    print("=" * 60)
    print("FINANCE LAW BUDGET YEAR COVERAGE")
    print("=" * 60)

    lf_years = sorted(set(l.get("annee_budgetaire") for l in laws if l.get("type") == "LF" and l.get("annee_budgetaire")))
    lfr_years = sorted(set(l.get("annee_budgetaire") for l in laws if l.get("type") == "LFR" and l.get("annee_budgetaire")))
    lfss_years = sorted(set(l.get("annee_budgetaire") for l in laws if l.get("type") == "LFSS" and l.get("annee_budgetaire")))

    print(f"\nLF covers {len(lf_years)} years: {lf_years[0]}-{lf_years[-1]}")
    missing_lf = set(range(lf_years[0], lf_years[-1] + 1)) - set(lf_years)
    if missing_lf:
        print(f"  Missing LF years: {sorted(missing_lf)}")
    else:
        print(f"  No gaps!")

    print(f"\nLFR covers {len(lfr_years)} years: {lfr_years[0]}-{lfr_years[-1]}")
    print(f"LFSS covers {len(lfss_years)} years: {lfss_years[0]}-{lfss_years[-1]}")

    # === Summary ===
    print()
    print("=" * 60)
    total_issues = len(errors) + len(bad_dates) + len(bad_types) + len(bad_annee) + len(bad_jorf)
    if total_issues == 0 and not without_jorf:
        print("ALL CHECKS PASSED")
    else:
        print(f"SUMMARY:")
        print(f"  API verification errors: {len(errors)}/{len(sample)}")
        print(f"  Bad dates: {len(bad_dates)}")
        print(f"  Bad types: {len(bad_types)}")
        print(f"  Bad annee_budgetaire: {len(bad_annee)}")
        print(f"  Bad JORF format: {len(bad_jorf)}")
        print(f"  Laws without JORF: {len(without_jorf)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
