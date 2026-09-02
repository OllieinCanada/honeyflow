"""Human jury prior system.

Loads aggregated correction factors from past jury votes and applies them
to AI-generated scores during graph building, creating a feedback loop
where human judgment progressively improves AI attribution decisions.
"""

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.database import get_session
from app.models.jury_prior import JuryPrior
from app.services.attribution_context import record_human_priors

logger = logging.getLogger(__name__)

PriorMap = Dict[str, Dict[str, Any]]


async def load_priors(entity_type: Optional[str] = None) -> PriorMap:
    """Load human priors from the database.

    Returns a dict keyed by ``"entity_type:entity_name_lower"`` with
    ``{"correction": float, "vote_count": int}``.
    """
    async with get_session() as session:
        stmt = select(JuryPrior)
        if entity_type:
            stmt = stmt.where(JuryPrior.entity_type == entity_type)
        rows = (await session.execute(stmt)).scalars().all()

    priors: PriorMap = {}
    for row in rows:
        key = "{}:{}".format(row.entity_type, row.entity_name.lower())
        priors[key] = {
            "correction": row.avg_correction,
            "vote_count": row.vote_count,
        }

    if priors:
        logger.info(
            "[PRIORS] Loaded %d human priors (type=%s)",
            len(priors),
            entity_type or "all",
        )
    record_human_priors(priors, entity_type)
    return priors


def apply_prior(
    raw_score: float,
    entity_name: str,
    entity_type: str,
    priors: PriorMap,
    min_votes: int = 2,
) -> float:
    """Blend a raw AI score with the human correction prior.

    The correction is dampened based on vote count so that a single vote
    barely nudges the score while 10+ votes apply nearly the full
    correction.
    """
    key = "{}:{}".format(entity_type, entity_name.lower())
    prior = priors.get(key)
    if not prior or prior["vote_count"] < min_votes:
        return raw_score

    correction = prior["correction"]
    strength = min(prior["vote_count"] / 10.0, 0.8)
    dampened = 1.0 + (correction - 1.0) * strength
    adjusted = raw_score * dampened

    if abs(dampened - 1.0) > 0.01:
        logger.debug(
            "[PRIORS] %s:%s correction=%.3f strength=%.2f %.4f->%.4f",
            entity_type,
            entity_name,
            correction,
            strength,
            raw_score,
            adjusted,
        )

    return max(adjusted, 0.0)


def apply_priors_to_scores(
    scores: Dict[str, float],
    entity_type: str,
    priors: PriorMap,
    min_votes: int = 2,
) -> Dict[str, float]:
    """Apply human priors to a dict of {name: score}."""
    return {
        name: apply_prior(score, name, entity_type, priors, min_votes)
        for name, score in scores.items()
    }
