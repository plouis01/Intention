#!/usr/bin/env python3
"""Add more Sénat debates for key 150-0 B ter modifications."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.collectors.senat_debats import SenatDebatCollector
from src.db.models import Debat, Intervention, Loi, get_session
from src.parsers.debates import extract_article_mentions


# Additional Sénat debates to fetch
# Focus on laws that modified 150-0 B ter
SENAT_DEBATES = [
    # LF 2014 (2013-1278) - modifications au régime
    {
        "url": "https://www.senat.fr/seances/s201311/s20131121/s20131121_mono.html",
        "date": datetime(2013, 11, 21),
        "description": "PLF 2014 - Discussion générale",
    },
    {
        "url": "https://www.senat.fr/seances/s201311/s20131122/s20131122_mono.html",
        "date": datetime(2013, 11, 22),
        "description": "PLF 2014 - Suite",
    },
    # LFR 2015 (2015-1786) - réinvestissement dans les fonds
    {
        "url": "https://www.senat.fr/seances/s201512/s20151210/s20151210_mono.html",
        "date": datetime(2015, 12, 10),
        "description": "PLFR 2015 - Discussion",
    },
    {
        "url": "https://www.senat.fr/seances/s201512/s20151211/s20151211_mono.html",
        "date": datetime(2015, 12, 11),
        "description": "PLFR 2015 - Suite et vote",
    },
    # LF 2018 (2017-1837) - aménagements
    {
        "url": "https://www.senat.fr/seances/s201711/s20171123/s20171123_mono.html",
        "date": datetime(2017, 11, 23),
        "description": "PLF 2018 - Première partie",
    },
    {
        "url": "https://www.senat.fr/seances/s201711/s20171124/s20171124_mono.html",
        "date": datetime(2017, 11, 24),
        "description": "PLF 2018 - Suite",
    },
    # LF 2019 (2018-1317) - passage 50% → 60%
    {
        "url": "https://www.senat.fr/seances/s201811/s20181122/s20181122_mono.html",
        "date": datetime(2018, 11, 22),
        "description": "PLF 2019 - Discussion",
    },
    {
        "url": "https://www.senat.fr/seances/s201811/s20181123/s20181123_mono.html",
        "date": datetime(2018, 11, 23),
        "description": "PLF 2019 - Suite",
    },
    # LF 2020 (2019-1479)
    {
        "url": "https://www.senat.fr/seances/s201911/s20191121/s20191121_mono.html",
        "date": datetime(2019, 11, 21),
        "description": "PLF 2020 - Discussion",
    },
    # LF 2021 (2020-1721)
    {
        "url": "https://www.senat.fr/seances/s202011/s20201119/s20201119_mono.html",
        "date": datetime(2020, 11, 19),
        "description": "PLF 2021 - Discussion",
    },
    # LF 2022 (2021-1900)
    {
        "url": "https://www.senat.fr/seances/s202111/s20211118/s20211118_mono.html",
        "date": datetime(2021, 11, 18),
        "description": "PLF 2022 - Discussion",
    },
    # LF 2023 (2022-1726)
    {
        "url": "https://www.senat.fr/seances/s202211/s20221117/s20221117_mono.html",
        "date": datetime(2022, 11, 17),
        "description": "PLF 2023 - Discussion",
    },
]


def add_senat_debates():
    """Fetch and add Sénat debates."""
    session = get_session()
    collector = SenatDebatCollector()

    print(f"Adding {len(SENAT_DEBATES)} Sénat debates...\n")

    added = 0
    for debate_info in SENAT_DEBATES:
        url = debate_info["url"]

        # Check if already exists
        existing = session.query(Debat).filter_by(url_source=url).first()
        if existing:
            print(f"  = {debate_info['description']} (exists)")
            continue

        print(f"Fetching: {debate_info['description']}...")

        result = collector.fetch_seance(url)

        if not result:
            print(f"  ! Failed to fetch")
            continue

        # Create debate record
        ref = f"senat_{debate_info['date'].strftime('%Y%m%d')}"
        debat = Debat(
            chambre="SENAT",
            date=debate_info["date"],
            etape="seance_publique",
            url_source=url,
            reference_externe=ref,
        )
        session.add(debat)
        session.flush()

        # Add interventions
        count = 0
        for i, interv in enumerate(result.get("interventions", [])):
            intervention = Intervention(
                debat_id=debat.id,
                orateur=interv["orateur"],
                fonction=interv.get("fonction"),
                contenu=interv["contenu"],
                articles_mentionnes=extract_article_mentions(interv["contenu"]),
                ordre=i + 1,
            )
            session.add(intervention)
            count += 1

        print(f"  + Added with {count} interventions")
        added += 1

        # Commit every 3 debates
        if added % 3 == 0:
            session.commit()

    session.commit()
    collector.close()

    # Summary
    print(f"\n--- Summary ---")
    print(f"Added {added} new Sénat debates")

    total_senat = session.query(Debat).filter_by(chambre="SENAT").count()
    print(f"Total Sénat debates: {total_senat}")

    session.close()


if __name__ == "__main__":
    add_senat_debates()
