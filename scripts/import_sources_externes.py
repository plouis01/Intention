#!/usr/bin/env python3
"""Import external sources for the 150-0 B ter analysis into the database."""

import sys
from datetime import datetime
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, SourceExterne


def get_session():
    """Create database session."""
    engine = create_engine("sqlite:///data/legislateur.db")
    Base.metadata.create_all(engine)  # Create tables if not exist
    Session = sessionmaker(bind=engine)
    return Session()


def import_sources():
    """Import all external sources used in the 150-0 B ter analysis."""

    session = get_session()
    now = datetime.now()

    sources = [
        # Sénat séances
        {
            "type": "seance",
            "titre": "Séance du 15 décembre 2016 - LFR 2016",
            "url": "https://www.senat.fr/seances/s201612/s20161215/s20161215023.html",
            "date": datetime(2016, 12, 15),
            "chambre": "SENAT",
            "reference": "Séance 15/12/2016",
            "auteur": "M. Albéric de Montgolfier (rapporteur), M. Richard Yung, M. Christophe Sirugue",
            "resume": "Débats sur les articles 32 et 33 de la LFR 2016 relatifs à l'imposition des soultes et aux opérations successives dans le cadre du régime d'apport-cession (art. 150-0 B ter).",
            "articles_cgi_lies": ["150-0 B ter"],
            "lois_liees": ["2016-1918"],
        },
        {
            "type": "seance",
            "titre": "Séance du 24 novembre 2023 - PLF 2024",
            "url": "https://www.senat.fr/seances/s202311/s20231124/s20231124020.html",
            "date": datetime(2023, 11, 24),
            "chambre": "SENAT",
            "reference": "Séance 24/11/2023",
            "auteur": "Mme Christine Lavarde, M. Michel Canévet, M. Emmanuel Capus, M. Jean-François Husson (rapporteur), M. Thomas Cazenave (ministre)",
            "resume": "Adoption des amendements I-627 rect. ter et I-986 rect. bis clarifiant le quota de 75% pour les fonds de capital-investissement éligibles au réinvestissement.",
            "articles_cgi_lies": ["150-0 B ter"],
            "lois_liees": ["2023-1322"],
        },
        # Sénat rapports
        {
            "type": "rapport",
            "titre": "Rapport n° 213 - LFR 2012 (création art. 150-0 B ter)",
            "url": "https://www.senat.fr/rap/l12-213-1/l12-213-129.html",
            "date": datetime(2012, 12, 1),
            "chambre": "SENAT",
            "reference": "Rapport n° 213",
            "auteur": "M. François Marc (rapporteur général)",
            "resume": "Rapport de la commission des finances sur la LFR 2012. Analyse de l'article 18 créant l'article 150-0 B ter pour mettre fin aux schémas d'apport-cession abusifs.",
            "articles_cgi_lies": ["150-0 B ter"],
            "lois_liees": ["2012-1510"],
        },
        {
            "type": "rapport",
            "titre": "Rapport n° 214 - LFR 2016",
            "url": "https://www.senat.fr/rap/l16-214-1/l16-214-1_mono.html",
            "date": datetime(2016, 12, 1),
            "chambre": "SENAT",
            "reference": "Rapport n° 214",
            "auteur": "M. Albéric de Montgolfier (rapporteur général)",
            "resume": "Rapport sur la LFR 2016. Articles 32 et 33 sur l'imposition des soultes et le maintien du report dans les opérations successives.",
            "articles_cgi_lies": ["150-0 B ter"],
            "lois_liees": ["2016-1918"],
        },
        {
            "type": "rapport",
            "titre": "Rapport n° 147 - PLF 2019 (50% → 60%)",
            "url": "https://www.senat.fr/rap/l18-147-3-1/l18-147-3-1.html",
            "date": datetime(2018, 11, 22),
            "chambre": "SENAT",
            "reference": "Rapport n° 147",
            "auteur": "Commission des finances",
            "resume": "Rapport sur le PLF 2019. Article 51 quater sur l'aménagement du champ de réinvestissement, passage du seuil de 50% à 60%.",
            "articles_cgi_lies": ["150-0 B ter"],
            "lois_liees": ["2018-1317"],
        },
        {
            "type": "rapport",
            "titre": "Rapport n° 140 - PLF 2020",
            "url": "https://www.senat.fr/rap/l19-140-3-1/l19-140-3-13.html",
            "date": datetime(2019, 11, 21),
            "chambre": "SENAT",
            "reference": "Rapport n° 140",
            "auteur": "Commission des finances",
            "resume": "Rapport sur le PLF 2020. Analyse du mécanisme d'apport-cession et de son élargissement au réinvestissement indirect via les fonds.",
            "articles_cgi_lies": ["150-0 B ter"],
            "lois_liees": ["2019-1479"],
        },
        # Sénat amendements
        {
            "type": "amendement_senat",
            "titre": "Amendement I-986 rect. bis - PLF 2024",
            "url": "https://www.senat.fr/amendements/2023-2024/127/Amdt_I-986.html",
            "date": datetime(2023, 11, 24),
            "chambre": "SENAT",
            "reference": "I-986 rect. bis",
            "auteur": "Mme Paoli-Gagin, M. Capus et 11 autres sénateurs",
            "resume": "Amendement modifiant le quota de 75% pour les fonds de capital-investissement, permettant les quasi-fonds propres (10%) et introduisant la perte proportionnelle du report. Adopté avec avis de sagesse du gouvernement.",
            "articles_cgi_lies": ["150-0 B ter"],
            "lois_liees": ["2023-1322"],
            "metadata": {"sort": "adopte", "avis_commission": "favorable", "avis_gouvernement": "sagesse"},
        },
        {
            "type": "amendement_senat",
            "titre": "Amendement I-627 rect. ter - PLF 2024",
            "url": "https://www.senat.fr/amendements/2023-2024/127/Amdt_I-627.html",
            "date": datetime(2023, 11, 24),
            "chambre": "SENAT",
            "reference": "I-627 rect. ter",
            "auteur": "Mme Christine Lavarde",
            "resume": "Amendement identique à I-986 rect. bis sur le quota de 75% pour les fonds. Adopté.",
            "articles_cgi_lies": ["150-0 B ter"],
            "lois_liees": ["2023-1322"],
            "metadata": {"sort": "adopte", "avis_commission": "favorable", "avis_gouvernement": "sagesse"},
        },
        # BOFIP
        {
            "type": "bofip",
            "titre": "BOI-RPPM-PVBMI-30-10-60 - Régime du report d'imposition",
            "url": "https://bofip.impots.gouv.fr/bofip/12040-PGP.html",
            "date": datetime(2020, 8, 18),
            "chambre": None,
            "reference": "BOI-RPPM-PVBMI-30-10-60",
            "auteur": "DGFiP",
            "resume": "Doctrine administrative sur le régime du report d'imposition prévu à l'article 150-0 B ter. Conditions, obligation de réinvestissement, V bis (opérations successives), événements mettant fin au report.",
            "articles_cgi_lies": ["150-0 B ter", "150-0 B", "150-0 B bis"],
            "lois_liees": None,
        },
        # Jurisprudence - Conseil d'État
        {
            "type": "jurisprudence",
            "titre": "CE, 16 février 2024, n° 472835",
            "url": "https://www.legifrance.gouv.fr/ceta/id/CETATEXT000049132548",
            "date": datetime(2024, 2, 16),
            "chambre": "CE",
            "reference": "CE 472835",
            "auteur": "Conseil d'État",
            "resume": "Les conditions permettant le maintien du report d'imposition s'apprécient à la date du réinvestissement, non à la date de l'apport. La condition relative au contrôle s'apprécie au moment où la holding acquiert les titres.",
            "articles_cgi_lies": ["150-0 B ter"],
            "lois_liees": None,
        },
        {
            "type": "jurisprudence",
            "titre": "CE, 19 avril 2022, n° 442946",
            "url": "https://www.legifrance.gouv.fr/ceta/id/CETATEXT000045578159",
            "date": datetime(2022, 4, 19),
            "chambre": "CE",
            "reference": "CE 442946",
            "auteur": "Conseil d'État",
            "resume": "Le réinvestissement dans une activité de location meublée ne relève pas en soi d'une activité à caractère économique. Seules les activités de parahôtellerie ou impliquant d'importants moyens matériels et humains sont admises.",
            "articles_cgi_lies": ["150-0 B ter"],
            "lois_liees": None,
        },
        # Jurisprudence - Conseil constitutionnel
        {
            "type": "jurisprudence",
            "titre": "CC, Décision n° 2019-775 QPC, 12 avril 2019",
            "url": "https://www.conseil-constitutionnel.fr/decision/2019/2019775QPC.htm",
            "date": datetime(2019, 4, 12),
            "chambre": "CC",
            "reference": "2019-775 QPC",
            "auteur": "Conseil constitutionnel",
            "resume": "Décision déclarant conformes à la Constitution les dispositions de l'article 150-0 B ter. Le report d'imposition constitue une incitation légitime aux restructurations d'entreprises.",
            "articles_cgi_lies": ["150-0 B ter"],
            "lois_liees": None,
        },
        # CADF
        {
            "type": "cadf",
            "titre": "CADF, Affaire 2012-34 (M. et Mme H.) - 27 septembre 2012",
            "url": "https://www.impots.gouv.fr/sites/default/files/media/3_Documentation/cadf/seance_du_27_septembre_2012.pdf",
            "date": datetime(2012, 9, 27),
            "chambre": "CADF",
            "reference": "Affaire 2012-34",
            "auteur": "Comité de l'abus de droit fiscal",
            "resume": "Le Comité a estimé que la souscription de parts de FCPR caractérisait un réinvestissement significatif dans une activité économique. L'administration n'était pas fondée à mettre en œuvre l'abus de droit. Décision ultérieurement légalisée par la LFR 2015.",
            "articles_cgi_lies": ["150-0 B ter", "150-0 B"],
            "lois_liees": None,
        },
    ]

    # Check for existing sources and add new ones
    added = 0
    for source_data in sources:
        existing = session.query(SourceExterne).filter_by(url=source_data["url"]).first()
        if not existing:
            source = SourceExterne(
                type=source_data["type"],
                titre=source_data["titre"],
                url=source_data["url"],
                date=source_data.get("date"),
                chambre=source_data.get("chambre"),
                reference=source_data.get("reference"),
                auteur=source_data.get("auteur"),
                resume=source_data.get("resume"),
                articles_cgi_lies=source_data.get("articles_cgi_lies"),
                lois_liees=source_data.get("lois_liees"),
                date_collecte=now,
                extra_data=source_data.get("metadata"),
            )
            session.add(source)
            added += 1
            print(f"  + {source_data['titre']}")
        else:
            print(f"  = {source_data['titre']} (exists)")

    session.commit()
    print(f"\nImported {added} new sources.")

    # Show summary
    total = session.query(SourceExterne).count()
    print(f"Total sources in database: {total}")

    session.close()


if __name__ == "__main__":
    print("Importing external sources for 150-0 B ter analysis...\n")
    import_sources()
