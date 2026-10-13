"""Amendment genealogy tracker for linking related amendments across stages."""

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import or_

from src.db.models import Amendement, DossierLegislatif, Loi, get_session

logger = logging.getLogger(__name__)


@dataclass
class AmendmentStage:
    """A single amendment at one legislative stage."""
    numero: str
    auteur: str
    sort: str  # adopte, rejete, retire, tombe
    stage: str  # commission, seance_publique
    chambre: str  # AN, SENAT
    objet: str = ""
    date_depot: str = ""


@dataclass
class AmendmentChain:
    """A chain of related amendments across stages."""
    stages: list[AmendmentStage] = field(default_factory=list)
    text_similarity: float = 0.0  # Jaccard similarity between first and last

    @property
    def outcome(self) -> str:
        """Final outcome of the chain."""
        if not self.stages:
            return "inconnu"
        return self.stages[-1].sort

    @property
    def summary(self) -> str:
        """Human-readable summary of the chain."""
        if not self.stages:
            return ""
        parts = []
        for s in self.stages:
            parts.append(f"{s.numero} ({s.sort}, {s.stage})")
        return " → ".join(parts)


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    """Compute Jaccard similarity between two texts based on word sets."""
    if not text_a or not text_b:
        return 0.0
    words_a = set(re.findall(r'\w+', text_a.lower()))
    words_b = set(re.findall(r'\w+', text_b.lower()))
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _author_overlap(auteur_a: str, auteur_b: str) -> bool:
    """Check if two amendment author strings share any names."""
    if not auteur_a or not auteur_b:
        return False
    # Extract capitalized words as potential names
    names_a = set(w for w in re.findall(r'[A-ZÀ-Ü][a-zà-ü]+', auteur_a) if len(w) > 2)
    names_b = set(w for w in re.findall(r'[A-ZÀ-Ü][a-zà-ü]+', auteur_b) if len(w) > 2)
    return bool(names_a & names_b)


class AmendmentTracker:
    """Track amendment genealogy across legislative stages."""

    def find_genealogy(
        self, law_numero: str, article_ref: str, similarity_threshold: float = 0.4
    ) -> list[AmendmentChain]:
        """
        Find amendment genealogy for a given article in a specific law.

        Groups amendments by text similarity + author overlap to build chains
        showing how amendments evolved across stages.

        Args:
            law_numero: Law number (e.g., "2023-1322")
            article_ref: CGI article reference (e.g., "150-0 B ter")
            similarity_threshold: Minimum Jaccard similarity to group amendments

        Returns:
            List of AmendmentChain showing stage transitions
        """
        # Query all amendments for this law mentioning the article
        ref_pattern = f"%{article_ref}%"
        try:
            with get_session() as session:
                amendments = (
                    session.query(Amendement)
                    .join(DossierLegislatif, Amendement.dossier_id == DossierLegislatif.id)
                    .join(Loi, DossierLegislatif.loi_id == Loi.id)
                    .filter(
                        Loi.numero == law_numero,
                        or_(
                            Amendement.objet.ilike(ref_pattern),
                            Amendement.expose_motifs.ilike(ref_pattern),
                        ),
                    )
                    .order_by(Amendement.date_depot)
                    .all()
                )
        except Exception as e:
            logger.error(f"Failed to query amendments for {law_numero}/{article_ref}: {e}")
            return []

        if not amendments:
            return []

        # Convert to AmendmentStage objects
        stages = []
        for amend in amendments:
            dossier = amend.dossier
            chambre = dossier.chambre if dossier else "?"
            # Infer stage from dossier etapes or amendment context
            stage = "seance_publique"  # default
            numero_lower = (amend.numero or "").lower()
            if numero_lower.startswith("cf") or numero_lower.startswith("com"):
                stage = "commission"

            stages.append(AmendmentStage(
                numero=amend.numero or "?",
                auteur=amend.auteur or "",
                sort=amend.sort or "inconnu",
                stage=stage,
                chambre=chambre,
                objet=amend.objet or "",
                date_depot=str(amend.date_depot.date()) if amend.date_depot else "",
            ))

        # Group into chains by text similarity + author overlap
        chains = self._group_into_chains(stages, similarity_threshold)
        return chains

    def _group_into_chains(
        self, stages: list[AmendmentStage], threshold: float
    ) -> list[AmendmentChain]:
        """Group amendments into chains based on text similarity and author overlap."""
        if not stages:
            return []

        # Track which stages have been assigned to a chain
        assigned = set()
        chains = []

        for i, stage_a in enumerate(stages):
            if i in assigned:
                continue

            chain_stages = [stage_a]
            assigned.add(i)

            for j, stage_b in enumerate(stages):
                if j in assigned or j <= i:
                    continue

                # Check text similarity on objet
                sim = _jaccard_similarity(stage_a.objet, stage_b.objet)
                author_match = _author_overlap(stage_a.auteur, stage_b.auteur)

                # Group if similar text OR same author with moderate similarity
                if sim >= threshold or (author_match and sim >= threshold * 0.6):
                    chain_stages.append(stage_b)
                    assigned.add(j)

            chain = AmendmentChain(
                stages=chain_stages,
                text_similarity=_jaccard_similarity(
                    chain_stages[0].objet,
                    chain_stages[-1].objet,
                ) if len(chain_stages) > 1 else 1.0,
            )
            chains.append(chain)

        return chains
