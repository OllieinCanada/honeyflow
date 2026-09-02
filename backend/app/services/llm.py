"""LLM integration via 0G Compute Network.

Calls the Next.js API route at /api/inference which handles
0G broker lifecycle, prompt construction, and inference.
Falls back to heuristic values when the inference API is unavailable.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(90.0, connect=10.0)

# Serialize calls to avoid overwhelming the 0G provider
_infer_lock = asyncio.Lock()


async def _call_inference(action: str, params: Dict[str, Any], label: str = "") -> Optional[Any]:
    """Call the Next.js 0G inference API. Returns parsed JSON result or None."""
    async with _infer_lock:
        logger.info("[LLM] Calling 0G inference (%s) action=%s ...", label, action)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    settings.inference_api_url,
                    json={"action": action, "params": params},
                )
                if resp.status_code >= 400:
                    logger.warning(
                        "[LLM] Inference API %d (%s): %s",
                        resp.status_code, label, resp.text[:300],
                    )
                    return None

                data = resp.json()
                result = data.get("result")
                if result is None:
                    logger.warning("[LLM] Inference returned null (%s)", label)
                    return None

                from app.services.attribution_context import stage_inference_metadata

                stage_inference_metadata(action, data.get("provenance"))

                logger.info("[LLM] 0G %s OK", label)
                return result
        except Exception as exc:
            logger.warning("[LLM] Inference API call failed (%s): %s", label, exc)
            return None


def _extract_float(d: Dict, keys: tuple) -> Optional[float]:
    """Try multiple key names to extract a float from a dict."""
    for k in keys:
        val = d.get(k)
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                continue
    return None


# ------------------------------------------------------------------
# Repo analysis
# ------------------------------------------------------------------

async def analyze_repo(
    readme: str,
    metadata: Dict[str, Any],
    file_tree: str,
) -> Dict[str, Any]:
    """Ask 0G to classify the repo's purpose and tech stack."""
    result = await _call_inference("analyze_repo", {
        "readme": readme or "",
        "description": metadata.get("description", ""),
        "languages": metadata.get("languages", {}),
        "file_tree": file_tree or "",
    }, label="analyze_repo")

    if isinstance(result, dict) and "purpose" in result:
        from app.services.attribution_context import record_contributing_inference

        record_contributing_inference("analyze_repo")
        logger.info("[LLM] Repo analysis: purpose=%s, type=%s",
                    result.get("purpose", "")[:80], result.get("project_type"))
        return result

    logger.info("[LLM] Using heuristic repo analysis (no LLM response)")
    return {
        "purpose": metadata.get("description", "Unknown project"),
        "tech_stack": list((metadata.get("languages") or {}).keys())[:5],
        "project_type": "application",
    }


# ------------------------------------------------------------------
# Direct-vs-dependencies split
# ------------------------------------------------------------------

async def split_direct_vs_deps(
    repo_analysis: Dict[str, Any],
    dep_count: int,
    source_file_count: int,
) -> Tuple[float, float]:
    """Ask 0G what fraction of value comes from custom code vs deps."""
    result = await _call_inference("split_direct_vs_deps", {
        "purpose": repo_analysis.get("purpose", "Unknown"),
        "project_type": repo_analysis.get("project_type", "application"),
        "tech_stack": repo_analysis.get("tech_stack", []),
        "source_file_count": source_file_count,
        "dep_count": dep_count,
    }, label="direct_vs_deps")

    if isinstance(result, dict):
        logger.info("[LLM] direct_vs_deps response parsed")
        direct = _extract_float(result, ("direct_fraction", "direct", "custom_code", "original_code"))
        deps = _extract_float(result, ("deps_fraction", "deps", "dependencies", "dependency_fraction"))
        if direct is not None and deps is not None:
            total = direct + deps
            if total > 0:
                d, p = (direct / total, deps / total)
                from app.services.attribution_context import record_contributing_inference

                record_contributing_inference("split_direct_vs_deps")
                logger.info("[LLM] Split -> direct=%.1f%% deps=%.1f%%", d * 100, p * 100)
                return (d, p)
        logger.warning("[LLM] Could not extract valid inference fractions")

    if dep_count == 0:
        logger.info("[LLM] No deps -> direct=100%%")
        return (1.0, 0.0)
    logger.info("[LLM] Using heuristic split -> direct=60%% deps=40%%")
    return (0.6, 0.4)


# ------------------------------------------------------------------
# Dependency importance ranking
# ------------------------------------------------------------------

async def rank_dependency_importance(
    repo_analysis: Dict[str, Any],
    dep_names: List[str],
    usage_freq: Dict[str, int],
) -> Dict[str, float]:
    """Ask 0G to rate each dependency's importance, then blend with usage frequency."""
    if not dep_names:
        return {}

    deps_data = [
        {"name": name, "import_count": usage_freq.get(name, 0)}
        for name in dep_names[:40]
    ]

    llm_scores = await _call_inference("rank_dependency_importance", {
        "purpose": repo_analysis.get("purpose", "Unknown"),
        "project_type": repo_analysis.get("project_type", "application"),
        "tech_stack": repo_analysis.get("tech_stack", []),
        "deps": deps_data,
    }, label="dep_ranking")

    if not isinstance(llm_scores, dict):
        logger.info("[LLM] No LLM dep scores, using usage frequency only")
        llm_scores = {}
    else:
        from app.services.attribution_context import record_contributing_inference

        record_contributing_inference("rank_dependency_importance")
        logger.info("[LLM] Got LLM dep scores for %d/%d deps", len(llm_scores), len(dep_names))

    max_freq = max(usage_freq.values()) if usage_freq else 1
    w_usage = settings.graph.usage_freq_weight
    w_llm = settings.graph.llm_importance_weight
    blended: Dict[str, float] = {}

    for name in dep_names:
        norm_usage = (usage_freq.get(name, 0) / max_freq) if max_freq > 0 else 0.0
        llm_score = llm_scores.get(name, 0.3)
        if not isinstance(llm_score, (int, float)):
            llm_score = 0.3
        llm_score = max(0.0, min(1.0, float(llm_score)))
        blended[name] = (w_usage * norm_usage) + (w_llm * llm_score)

    try:
        from app.services.jury_priors import apply_priors_to_scores, load_priors
        priors = await load_priors("dependency")
        if priors:
            blended = apply_priors_to_scores(blended, "dependency", priors)
            logger.info("[LLM] Applied human priors to %d dependency scores", len(blended))
    except Exception as exc:
        logger.debug("[LLM] Could not apply dependency priors: %s", exc)

    return blended


# ------------------------------------------------------------------
# Package analysis
# ------------------------------------------------------------------

async def analyze_package(
    description: str,
    keywords: List[str],
    dep_names: List[str],
    readme: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Ask 0G to classify a package's purpose and tech stack."""
    metadata = metadata or {}

    result = await _call_inference("analyze_package", {
        "description": description or "",
        "keywords": keywords or [],
        "dep_names": dep_names or [],
        "readme": readme or "",
        "languages": metadata.get("languages", {}),
    }, label="analyze_package")

    if isinstance(result, dict) and "purpose" in result:
        from app.services.attribution_context import record_contributing_inference

        record_contributing_inference("analyze_package")
        logger.info("[LLM] Package analysis: purpose=%s, type=%s",
                    result.get("purpose", "")[:80], result.get("project_type"))
        return result

    logger.info("[LLM] Using heuristic package analysis (no LLM response)")
    return {
        "purpose": description or "Unknown package",
        "tech_stack": keywords[:5] if keywords else [],
        "project_type": "library",
    }


# ------------------------------------------------------------------
# Citation influence ranking
# ------------------------------------------------------------------

async def rank_citation_influence(
    paper_title: str,
    paper_abstract: str,
    categories: List[str],
    citations: List[Dict[str, Any]],
) -> Dict[str, float]:
    """Ask 0G to rank citations by intellectual influence."""
    if not citations:
        return {}

    result = await _call_inference("rank_citation_influence", {
        "paper_title": paper_title,
        "paper_abstract": paper_abstract,
        "categories": categories,
        "citations": citations[:30],
    }, label="citation_influence")

    if isinstance(result, dict):
        from app.services.attribution_context import record_contributing_inference

        record_contributing_inference("rank_citation_influence")
        logger.info("[LLM] Got influence scores for %d/%d citations",
                    len(result), len(citations))
        scores: Dict[str, float] = {}
        for c in citations:
            key = c.get("key", "")
            score = result.get(key, 0.2)
            if not isinstance(score, (int, float)):
                score = 0.2
            scores[key] = max(0.0, min(1.0, float(score)))

        try:
            from app.services.jury_priors import apply_priors_to_scores, load_priors
            priors = await load_priors("citation")
            if priors:
                scores = apply_priors_to_scores(scores, "citation", priors)
        except Exception:
            pass

        return scores

    logger.info("[LLM] No LLM influence scores, falling back to frequency-based ranking")
    max_freq = max((c.get("frequency", 1) for c in citations), default=1) or 1
    scores = {
        c.get("key", ""): c.get("frequency", 0) / max_freq
        for c in citations
    }

    try:
        from app.services.jury_priors import apply_priors_to_scores, load_priors
        priors = await load_priors("citation")
        if priors:
            scores = apply_priors_to_scores(scores, "citation", priors)
            logger.info("[LLM] Applied human priors to %d citation scores", len(scores))
    except Exception as exc:
        logger.debug("[LLM] Could not apply citation priors: %s", exc)

    return scores


async def analyze_paper(
    title: str,
    abstract: str,
    categories: List[str],
) -> Dict[str, Any]:
    """Ask 0G to classify a paper's contribution and research area."""
    result = await _call_inference("analyze_paper", {
        "title": title,
        "abstract": abstract,
        "categories": categories,
    }, label="analyze_paper")

    if isinstance(result, dict) and "contribution" in result:
        from app.services.attribution_context import record_contributing_inference

        record_contributing_inference("analyze_paper")
        logger.info("[LLM] Paper analysis: %s", result.get("contribution", "")[:80])
        return result

    return {
        "contribution": abstract[:200] if abstract else "Unknown",
        "research_area": categories[0] if categories else "unknown",
        "paper_type": "other",
    }
