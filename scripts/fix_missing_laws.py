#!/usr/bin/env python3
"""Add missing laws (2012-2018) to the database."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, Loi


def get_session():
    """Create database session."""
    engine = create_engine("sqlite:///data/legislateur.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def add_missing_laws():
    """Add laws from 2012-2018 that modified article 150-0 B ter."""

    session = get_session()

    laws = [
        {
            "numero": "2012-1510",
            "type": "LFR",
            "annee": 2012,
            "date_promulgation": datetime(2012, 12, 29),
            "titre": "Loi n° 2012-1510 du 29 décembre 2012 de finances rectificative pour 2012",
            "url_legifrance": "https://www.legifrance.gouv.fr/loda/id/JORFTEXT000026856853",
        },
        {
            "numero": "2013-1278",
            "type": "LF",
            "annee": 2014,
            "date_promulgation": datetime(2013, 12, 29),
            "titre": "Loi n° 2013-1278 du 29 décembre 2013 de finances pour 2014",
            "url_legifrance": "https://www.legifrance.gouv.fr/loda/id/JORFTEXT000028399511",
        },
        {
            "numero": "2015-1786",
            "type": "LFR",
            "annee": 2015,
            "date_promulgation": datetime(2015, 12, 29),
            "titre": "Loi n° 2015-1786 du 29 décembre 2015 de finances rectificative pour 2015",
            "url_legifrance": "https://www.legifrance.gouv.fr/loda/id/JORFTEXT000031732865",
        },
        {
            "numero": "2016-1918",
            "type": "LFR",
            "annee": 2016,
            "date_promulgation": datetime(2016, 12, 29),
            "titre": "Loi n° 2016-1918 du 29 décembre 2016 de finances rectificative pour 2016",
            "url_legifrance": "https://www.legifrance.gouv.fr/loda/id/JORFTEXT000033734169",
        },
        {
            "numero": "2017-1837",
            "type": "LF",
            "annee": 2018,
            "date_promulgation": datetime(2017, 12, 30),
            "titre": "Loi n° 2017-1837 du 30 décembre 2017 de finances pour 2018",
            "url_legifrance": "https://www.legifrance.gouv.fr/loda/id/JORFTEXT000036339197",
        },
        {
            "numero": "2018-1317",
            "type": "LF",
            "annee": 2019,
            "date_promulgation": datetime(2018, 12, 28),
            "titre": "Loi n° 2018-1317 du 28 décembre 2018 de finances pour 2019",
            "url_legifrance": "https://www.legifrance.gouv.fr/loda/id/JORFTEXT000037882341",
        },
    ]

    added = 0
    for law_data in laws:
        existing = session.query(Loi).filter_by(numero=law_data["numero"]).first()
        if not existing:
            loi = Loi(**law_data)
            session.add(loi)
            added += 1
            print(f"  + {law_data['numero']} - {law_data['titre'][:60]}...")
        else:
            print(f"  = {law_data['numero']} (exists)")

    session.commit()
    print(f"\nAdded {added} laws.")

    # Show all laws
    all_laws = session.query(Loi).order_by(Loi.date_promulgation).all()
    print(f"\nTotal laws in database: {len(all_laws)}")

    session.close()


if __name__ == "__main__":
    print("Adding missing laws (2012-2018)...\n")
    add_missing_laws()
