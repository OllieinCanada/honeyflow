"""Human jury endpoints — plain-language attribution questions."""

import asyncio
import hashlib
import json
import logging
import random
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.config import settings
from app.database import get_session, session_scope
from app.models.edge_vote import EdgeVote
from app.models.jury_prior import JuryPrior
from app.models.project import Project
from app.schemas.jury import (
    JuryCodeSample,
    JuryEdgeRef,
    JuryLink,
    JuryPeer,
    JuryQuestion,
    JuryQuestionsResponse,
    SubmitJuryAnswersRequest,
    SubmitJuryAnswersResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jury", tags=["jury"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _fmt_int(value: Any) -> str:
    try:
        return "{:,}".format(int(value))
    except Exception:
        return str(value)


def _question_type(
    source_type: str,
    target_type: str,
    target_label: str,
) -> str:
    if target_type in ("CONTRIBUTOR", "AUTHOR"):
        return "contributor"
    if source_type in ("PAPER", "CITED_WORK") and target_type == "CITED_WORK":
        return "citation"
    if target_type in ("BODY_OF_WORK", "CITED_WORK"):
        return "dependency"
    return ""


# ---------------------------------------------------------------------------
# Plain-English prompt generation
# ---------------------------------------------------------------------------


def _build_prompt(
    q_type: str,
    project_name: str,
    target_label: str,
) -> str:
    if q_type == "contributor":
        return "How much do you think {} contributed to {}?".format(
            target_label,
            project_name,
        )
    if q_type == "citation":
        return "How much did '{}' influence {}?".format(
            target_label,
            project_name,
        )
    return "How much does {} rely on {}?".format(
        project_name,
        target_label,
    )


# ---------------------------------------------------------------------------
# Subject summary — one-liner about the person/dep/citation being asked about
# ---------------------------------------------------------------------------


def _build_subject_summary(
    target_node: Dict[str, Any],
    edge: Dict[str, Any],
    q_type: str,
    peer_rank: int,
    total_peers: int,
) -> str:
    """Build a narrative, human-readable description of the subject."""
    target_meta = target_node.get("metadata") or {}
    edge_meta = edge.get("metadata") or {}

    if q_type == "contributor":
        commits = target_meta.get("total_commits") or edge_meta.get(
            "contributor_total_commits"
        )
        additions = target_meta.get("total_additions")
        deletions = target_meta.get("total_deletions")

        if peer_rank == 1 and total_peers > 1:
            rank_text = "The project's most active contributor"
        elif peer_rank == 2 and total_peers > 2:
            rank_text = "The second most active contributor"
        elif peer_rank <= 3 and total_peers > 3:
            rank_text = "One of the top contributors"
        elif total_peers > 1:
            rank_text = "Contributor #{} of {}".format(peer_rank, total_peers)
        else:
            rank_text = "A contributor to this project"

        work_bits: list[str] = []
        if commits is not None:
            work_bits.append("with {} commits".format(_fmt_int(commits)))
        if additions is not None and deletions is not None:
            add_val, del_val = int(additions), int(deletions)
            total = add_val + del_val
            if total > 0:
                ratio = add_val / total
                if ratio > 0.8:
                    work_bits.append("primarily writing new code and features")
                elif ratio < 0.3:
                    work_bits.append("focused on refactoring and cleanup")
                else:
                    work_bits.append("a mix of new features and maintenance")
        if work_bits:
            return "{}, {}.".format(rank_text, ", ".join(work_bits))
        return rank_text + "."

    if q_type == "dependency":
        purpose = ""
        if isinstance(target_meta, dict):
            purpose = str(target_meta.get("purpose", "")).strip()[:200]
        sentences: list[str] = []
        if purpose:
            sentences.append(purpose.rstrip("."))
        usage = edge_meta.get("usage_import_count")
        if usage is not None:
            sentences.append(
                "Imported across {} source files in the project".format(_fmt_int(usage))
            )
        if edge_meta.get("is_dev_dependency") is True:
            sentences.append("Used only during development, not in production")
        return (
            ". ".join(sentences) + "." if sentences else "A dependency of this project."
        )

    if q_type == "citation":
        purpose = ""
        if isinstance(target_meta, dict):
            raw = target_meta.get("purpose") or target_meta.get("title") or ""
            purpose = str(raw).strip()[:200]
        sentences = []
        if purpose:
            sentences.append(purpose.rstrip("."))
        explicit = target_meta.get("explicit_mentions")
        conceptual = target_meta.get("conceptual_mentions")
        if explicit is not None:
            sentences.append("Directly cited {} times".format(_fmt_int(explicit)))
        if conceptual is not None and int(conceptual) > 0:
            sentences.append("{} conceptual references".format(_fmt_int(conceptual)))
        return (
            ". ".join(sentences) + "."
            if sentences
            else "A reference cited in this work."
        )

    return ""


# ---------------------------------------------------------------------------
# Peer comparison list — all "alternatives" the user can mentally compare
# ---------------------------------------------------------------------------


def _build_peers(
    edge: Dict[str, Any],
    edges: List[Dict[str, Any]],
    node_by_id: Dict[str, Dict[str, Any]],
    q_type: str,
) -> List[JuryPeer]:
    source_id = edge.get("source")
    target_id = edge.get("target")
    siblings = [e for e in edges if e.get("source") == source_id]

    peers: List[JuryPeer] = []
    for sib in siblings:
        sib_target_id = str(sib.get("target", ""))
        sib_node = node_by_id.get(sib_target_id) or {}
        sib_meta = sib_node.get("metadata") or {}
        sib_edge_meta = sib.get("metadata") or {}
        weight = _clamp01(_safe_float(sib.get("weight"), 0.0))

        name = str(sib_node.get("label", sib_target_id))

        detail = _peer_detail(sib_meta, sib_edge_meta, q_type)

        peers.append(
            JuryPeer(
                name=name,
                ai_pct=round(weight * 100, 1),
                detail=detail,
                is_subject=(sib_target_id == str(target_id)),
            )
        )

    peers.sort(key=lambda p: -p.ai_pct)
    return peers


def _peer_detail(
    node_meta: Dict[str, Any],
    edge_meta: Dict[str, Any],
    q_type: str,
) -> str:
    if q_type == "contributor":
        parts: list[str] = []
        commits = node_meta.get("total_commits") or edge_meta.get(
            "contributor_total_commits"
        )
        if commits is not None and int(commits) > 0:
            parts.append("{} commits".format(_fmt_int(commits)))
        lines = node_meta.get("total_lines") or edge_meta.get("contributor_total_lines")
        if lines is not None and int(lines) > 0:
            parts.append("{} lines changed".format(_fmt_int(lines)))
        additions = node_meta.get("total_additions")
        deletions = node_meta.get("total_deletions")
        if additions is not None and deletions is not None:
            total = int(additions) + int(deletions)
            if total > 0:
                ratio = int(additions) / total
                if ratio > 0.75:
                    parts.append("mostly new code")
                elif ratio < 0.3:
                    parts.append("mostly refactoring")
        return " · ".join(parts) if parts else ""

    if q_type == "dependency":
        parts: list[str] = []
        purpose = str(node_meta.get("purpose", "")).strip()
        if purpose:
            parts.append(purpose[:50])
        usage = edge_meta.get("usage_import_count")
        if usage is not None:
            parts.append("{} files".format(_fmt_int(usage)))
        return " · ".join(parts)

    if q_type == "citation":
        purpose = str(node_meta.get("purpose") or node_meta.get("title") or "").strip()
        if purpose:
            return purpose[:60]
        explicit = node_meta.get("explicit_mentions")
        if explicit is not None:
            return "Cited {} times".format(_fmt_int(explicit))
        return ""

    return ""


# ---------------------------------------------------------------------------
# Resource links — clearly labeled, human-friendly
# ---------------------------------------------------------------------------


def _repo_url_from_node_id(node_id: str) -> Optional[str]:
    rest = ""
    if node_id.startswith("repo:"):
        rest = node_id[len("repo:") :]
    elif node_id.startswith("bow:"):
        rest = node_id[len("bow:") :]
    if not rest or "/" not in rest:
        return None
    owner, repo = rest.split("/", 1)
    repo = repo.split(":")[0]
    if owner in {"npm", "pypi", "crates", "go"}:
        return None
    if not owner or not repo:
        return None
    return "https://github.com/{}/{}".format(owner, repo)


def _arxiv_url_from_node(node: Dict[str, Any]) -> Optional[str]:
    metadata = node.get("metadata") or {}
    arxiv_url = metadata.get("arxiv_url")
    if isinstance(arxiv_url, str) and arxiv_url.strip():
        return arxiv_url.strip()
    node_id = str(node.get("id", ""))
    raw = ""
    if node_id.startswith("paper:"):
        raw = node_id[len("paper:") :]
    elif node_id.startswith("cited:"):
        raw = node_id[len("cited:") :]
        if raw.startswith("original:"):
            raw = raw[len("original:") :]
        raw = raw.split(":")[0]
    if not raw or not re.search(r"\d{4}\.\d{4,5}", raw):
        return None
    return "https://arxiv.org/abs/{}".format(raw)


def _build_links(
    project: Project,
    source_node: Dict[str, Any],
    target_node: Dict[str, Any],
    q_type: str,
) -> List[JuryLink]:
    links: List[JuryLink] = []
    seen: Set[str] = set()

    def add(label: str, url: Optional[str]) -> None:
        if not url or not isinstance(url, str):
            return
        clean = url.strip()
        if not clean or clean in seen:
            return
        seen.add(clean)
        links.append(JuryLink(label=label, url=clean))

    add("View project on GitHub", project.source_url)

    if target_node.get("type") == "CONTRIBUTOR":
        login = str(target_node.get("label", "")).strip()
        if re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
            add("{}'s profile".format(login), "https://github.com/{}".format(login))

    target_repo = _repo_url_from_node_id(str(target_node.get("id", "")))
    if target_repo:
        add("View dependency repo", target_repo)

    source_repo = _repo_url_from_node_id(str(source_node.get("id", "")))
    if source_repo and source_repo != project.source_url:
        add("Related repository", source_repo)

    arxiv = _arxiv_url_from_node(target_node)
    if arxiv:
        add("Read paper on arXiv", arxiv)
    arxiv_src = _arxiv_url_from_node(source_node)
    if arxiv_src:
        add("Read source paper", arxiv_src)

    source_meta = source_node.get("metadata") or {}
    target_meta = target_node.get("metadata") or {}
    if isinstance(target_meta, dict) and target_meta.get("source_url"):
        add("Reference link", str(target_meta["source_url"]))
    if isinstance(source_meta, dict) and source_meta.get("source_url"):
        add("Source documentation", str(source_meta["source_url"]))

    return links[:6]


# ---------------------------------------------------------------------------
# Node lookup helpers
# ---------------------------------------------------------------------------


def _node_lookup(nodes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    lookup: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        node_id = str(node.get("id", ""))
        if node_id:
            lookup[node_id] = node
    return lookup


def _find_edge(
    edges: List[Dict[str, Any]],
    source_id: str,
    target_id: str,
) -> Optional[Dict[str, Any]]:
    for edge in edges:
        if edge.get("source") == source_id and edge.get("target") == target_id:
            return edge
    return None


# ---------------------------------------------------------------------------
# Attribution recomputation (unchanged logic)
# ---------------------------------------------------------------------------


def _compute_leaf_attribution(graph_data: Dict[str, Any]) -> Dict[str, float]:
    nodes = graph_data.get("nodes") or []
    edges = graph_data.get("edges") or []
    node_by_id = _node_lookup(nodes)

    children: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for edge in edges:
        src = edge.get("source")
        dst = edge.get("target")
        if not src or not dst:
            continue
        children[src].append((dst, _clamp01(_safe_float(edge.get("weight"), 0.0))))

    root_id = None
    for node in nodes:
        if node.get("type") in ("REPO", "PACKAGE", "PAPER"):
            root_id = node.get("id")
            break
    if not root_id:
        return {}

    attribution: Dict[str, float] = defaultdict(float)
    leaf_types = {"CONTRIBUTOR", "AUTHOR"}
    stack: List[Tuple[str, float, Set[str]]] = [(root_id, 1.0, {root_id})]
    steps = 0
    max_steps = 1_000_000

    while stack:
        node_id, cumulative_weight, path_nodes = stack.pop()
        steps += 1
        if steps > max_steps:
            break
        node = node_by_id.get(node_id)
        if not node:
            continue
        if node.get("type") in leaf_types:
            label = str(node.get("label", "")).strip()
            if label:
                attribution[label] += cumulative_weight
            continue
        for child_id, edge_weight in children.get(node_id, []):
            if child_id in path_nodes:
                continue
            stack.append(
                (child_id, cumulative_weight * edge_weight, path_nodes | {child_id})
            )

    sorted_items = sorted(attribution.items(), key=lambda x: -x[1])
    return {k: round(v, 6) for k, v in sorted_items}


def _top_contributors(attribution: Dict[str, float]) -> List[Dict[str, str]]:
    if not attribution:
        return []
    total = sum(attribution.values()) or 1.0
    top = list(attribution.items())[:10]
    return [
        {"name": name, "percentage": "{:.1f}%".format((score / total) * 100)}
        for name, score in top
    ]


# ---------------------------------------------------------------------------
# Shared helpers for question generation
# ---------------------------------------------------------------------------


async def _fetch_candidate_edges(
    count: int,
) -> Tuple[list, list[Tuple], list[Tuple]]:
    """Two-phase DB fetch: lightweight metadata first, then graph_data for a subset.

    Returns (projects, proj_cache, edge_refs)
    """
    # Phase 1: lightweight columns only (no graph_data JSONB)
    _light_cols = load_only(
        Project.id, Project.name, Project.slug, Project.source_url,
        Project.summary, Project.description, Project.type,
    )
    async with get_session() as session:
        light_projects = (
            (
                await session.execute(
                    select(Project)
                    .options(_light_cols)
                    .order_by(Project.created_at.desc())
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )

    if not light_projects:
        return [], [], []

    # Pick a small random subset to actually load graph_data for
    random.shuffle(light_projects)
    pick_count = min(8, len(light_projects))
    picked_ids = [p.id for p in light_projects[:pick_count]]

    _graph_cols = load_only(
        Project.id, Project.name, Project.slug, Project.source_url,
        Project.summary, Project.description, Project.type,
        Project.graph_data,
    )
    async with get_session() as session:
        projects = (
            (
                await session.execute(
                    select(Project)
                    .options(_graph_cols)
                    .where(Project.id.in_(picked_ids))
                )
            )
            .scalars()
            .all()
        )

    # Scan edges
    edge_refs: list[Tuple] = []
    proj_cache: list[Tuple] = []

    for project in projects:
        graph_data = project.graph_data or {}
        nodes = graph_data.get("nodes") or []
        edges = graph_data.get("edges") or []
        if not nodes or not edges:
            continue
        node_by_id = _node_lookup(nodes)
        proj_cache.append((project, nodes, edges, node_by_id))

        for ei, edge in enumerate(edges):
            source_id = edge.get("source")
            target_id = edge.get("target")
            if not source_id or not target_id:
                continue
            source_node = node_by_id.get(source_id)
            target_node = node_by_id.get(target_id)
            if not source_node or not target_node:
                continue
            source_type = str(source_node.get("type", ""))
            target_type = str(target_node.get("type", ""))
            target_label = str(target_node.get("label", target_id))
            q_type = _question_type(source_type, target_type, target_label)
            if not q_type:
                continue
            ai_weight = _clamp01(_safe_float(edge.get("weight"), 0.0))
            if ai_weight < 0.05:
                continue
            edge_refs.append((len(proj_cache) - 1, ei))

    return projects, proj_cache, edge_refs


def _build_questions_from_refs(
    chosen: list[Tuple],
    proj_cache: list[Tuple],
) -> List[JuryQuestion]:
    """Build full JuryQuestion objects from lightweight edge references."""
    questions: List[JuryQuestion] = []
    for cache_idx, edge_idx in chosen:
        project, nodes, edges, node_by_id = proj_cache[cache_idx]
        edge = edges[edge_idx]

        source_id = edge.get("source")
        target_id = edge.get("target")
        source_node = node_by_id.get(source_id)
        target_node = node_by_id.get(target_id)
        source_type = str(source_node.get("type", ""))
        target_type = str(target_node.get("type", ""))
        target_label = str(target_node.get("label", target_id))
        q_type = _question_type(source_type, target_type, target_label)
        ai_weight = _clamp01(_safe_float(edge.get("weight"), 0.0))

        question_key = "{}:{}->{}".format(project.id, source_id, target_id)
        question_id = hashlib.sha1(
            question_key.encode("utf-8")
        ).hexdigest()[:16]

        root_meta: Dict[str, Any] = {}
        for node in nodes:
            if node.get("type") in ("REPO", "PACKAGE", "PAPER"):
                root_meta = node.get("metadata") or {}
                break
        ai_purpose = str(root_meta.get("purpose", "")).strip()
        tech_stack = root_meta.get("tech_stack")
        desc_parts: list[str] = []
        if ai_purpose:
            desc_parts.append(ai_purpose)
        elif project.summary:
            desc_parts.append(project.summary.strip())
        elif project.description:
            desc_parts.append(project.description.strip()[:200])
        else:
            desc_parts.append(
                "An open-source {} project.".format(project.type or "software")
            )
        if isinstance(tech_stack, list) and tech_stack:
            desc_parts.append(
                "Built with {}.".format(", ".join(str(t) for t in tech_stack[:5]))
            )
        project_desc = " ".join(desc_parts)

        prompt = _build_prompt(q_type, project.name, target_label)
        peers = _build_peers(edge, edges, node_by_id, q_type)
        peer_rank = next(
            (i + 1 for i, p in enumerate(peers) if p.is_subject), 0
        )
        subject_summary = _build_subject_summary(
            target_node, edge, q_type, peer_rank, len(peers),
        )
        links = _build_links(project, source_node, target_node, q_type)

        questions.append(
            JuryQuestion(
                question_id=question_id,
                prompt=prompt,
                project_name=project.name,
                project_id=project.id,
                project_slug=project.slug,
                project_description=project_desc[:300],
                project_url=project.source_url,
                subject_name=target_label,
                subject_summary=subject_summary,
                peers=peers[:15],
                total_peers=len(peers),
                links=links,
                edge=JuryEdgeRef(
                    source_id=source_id,
                    target_id=target_id,
                    ai_weight=round(ai_weight, 6),
                    ai_percentage=round(ai_weight * 100, 2),
                    question_type=q_type,
                ),
            )
        )
    return questions


# ---------------------------------------------------------------------------
# GET /jury/questions
# ---------------------------------------------------------------------------


@router.get("/questions", response_model=JuryQuestionsResponse)
async def get_jury_questions(
    count: int = Query(5, ge=1, le=7),
):
    projects, proj_cache, edge_refs = await _fetch_candidate_edges(count)

    if not edge_refs:
        return JuryQuestionsResponse(questions=[])

    sample_size = min(count, len(edge_refs))
    chosen = random.sample(edge_refs, sample_size)
    questions = _build_questions_from_refs(chosen, proj_cache)

    # Enrich concurrently
    code_enriched, stats_enriched = await asyncio.gather(
        _enrich_questions_with_code(list(questions)),
        _enrich_peer_stats(list(questions)),
        return_exceptions=True,
    )
    return JuryQuestionsResponse(
        questions=_merge_enrichments(questions, code_enriched, stats_enriched),
    )


# ---------------------------------------------------------------------------
# GET /jury/questions/stream — progressive SSE loading
# ---------------------------------------------------------------------------


def _jury_sse(event: str, data) -> str:
    payload = json.dumps(data) if not isinstance(data, str) else data
    return "event: {}\ndata: {}\n\n".format(event, payload)


def _merge_enrichments(
    questions: List[JuryQuestion],
    code_enriched,
    stats_enriched,
) -> List[JuryQuestion]:
    if isinstance(code_enriched, Exception):
        logger.warning("Code enrichment failed: %s", code_enriched)
        code_enriched = questions
    if isinstance(stats_enriched, Exception):
        logger.warning("Stats enrichment failed: %s", stats_enriched)
        stats_enriched = questions

    merged = []
    for i, q in enumerate(questions):
        updates: Dict[str, Any] = {}
        if i < len(code_enriched) and code_enriched[i].code_samples:
            updates["code_samples"] = code_enriched[i].code_samples
        if i < len(stats_enriched):
            if stats_enriched[i].peers != q.peers:
                updates["peers"] = stats_enriched[i].peers
            if stats_enriched[i].subject_summary != q.subject_summary:
                updates["subject_summary"] = stats_enriched[i].subject_summary
        merged.append(q.model_copy(update=updates) if updates else q)
    return merged


@router.get("/questions/stream")
async def stream_jury_questions(
    count: int = Query(5, ge=1, le=7),
):
    async def event_generator():
        yield _jury_sse("progress", {"pct": 10, "msg": ""})
        await asyncio.sleep(0)

        _, proj_cache, edge_refs = await _fetch_candidate_edges(count)

        if not edge_refs:
            yield _jury_sse("questions", {"questions": []})
            yield _jury_sse("done", "complete")
            return

        yield _jury_sse("progress", {"pct": 50, "msg": "Building questions..."})
        await asyncio.sleep(0)

        sample_size = min(count, len(edge_refs))
        chosen = random.sample(edge_refs, sample_size)
        questions = _build_questions_from_refs(chosen, proj_cache)

        # Send base questions immediately so the user can start answering
        yield _jury_sse("progress", {"pct": 60, "msg": "Ready!"})
        await asyncio.sleep(0)
        yield _jury_sse(
            "questions",
            JuryQuestionsResponse(questions=questions).model_dump(),
        )
        await asyncio.sleep(0)

        # Phase 4: Enrich with code samples in background (peer stats already in graph_data)
        yield _jury_sse("progress", {"pct": 80, "msg": "Loading code samples..."})
        await asyncio.sleep(0)

        code_enriched, stats_enriched = await asyncio.gather(
            _enrich_questions_with_code(list(questions)),
            _enrich_peer_stats(list(questions)),
            return_exceptions=True,
        )
        merged = _merge_enrichments(questions, code_enriched, stats_enriched)

        yield _jury_sse("progress", {"pct": 100, "msg": "Done"})
        await asyncio.sleep(0)
        yield _jury_sse(
            "questions",
            JuryQuestionsResponse(questions=merged).model_dump(),
        )
        yield _jury_sse("done", "complete")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Code snippet enrichment — fetch real commits for contributor questions
# ---------------------------------------------------------------------------


def _owner_repo_from_node_id(node_id: str) -> Optional[Tuple[str, str]]:
    url = _repo_url_from_node_id(node_id)
    if not url:
        return None
    parts = url.rstrip("/").split("/")
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return None


async def _enrich_questions_with_code(
    questions: List[JuryQuestion],
) -> List[JuryQuestion]:
    """Fetch recent commits with code patches for contributor questions."""
    from app.services.github import fetch_contributor_commits

    tasks: List[Tuple[int, str, str, str]] = []
    for i, q in enumerate(questions):
        if q.edge.question_type != "contributor":
            continue
        owner_repo = _owner_repo_from_node_id(q.edge.source_id)
        if not owner_repo:
            continue
        tasks.append((i, owner_repo[0], owner_repo[1], q.subject_name))

    if not tasks:
        return questions

    fetch_results = await asyncio.gather(
        *(
            fetch_contributor_commits(owner, repo, login, 3)
            for _, owner, repo, login in tasks
        ),
        return_exceptions=True,
    )

    enriched = list(questions)
    for (idx, _, _, _), result in zip(tasks, fetch_results):
        if isinstance(result, Exception):
            logger.warning("Code fetch failed for question %d: %s", idx, result)
            continue
        if not result:
            continue

        samples: List[JuryCodeSample] = []
        for commit_data in result:
            if commit_data.get("files"):
                for f in commit_data["files"]:
                    samples.append(
                        JuryCodeSample(
                            filename=f.get("filename", ""),
                            patch=f.get("patch", ""),
                            commit_message=commit_data.get("message", ""),
                            commit_url=commit_data.get("url", ""),
                        )
                    )
            else:
                samples.append(
                    JuryCodeSample(
                        commit_message=commit_data.get("message", ""),
                        commit_url=commit_data.get("url", ""),
                    )
                )

        enriched[idx] = enriched[idx].model_copy(update={"code_samples": samples[:5]})

    return enriched


# ---------------------------------------------------------------------------
# Peer stats enrichment — fetch from GitHub when graph data is sparse
# ---------------------------------------------------------------------------


async def _enrich_peer_stats(
    questions: List[JuryQuestion],
) -> List[JuryQuestion]:
    """Fetch contributor stats from GitHub for questions with empty peer details."""
    from app.services.github import fetch_contributor_stats

    needs: List[Tuple[int, str, str]] = []  # (question_idx, owner, repo)
    for i, q in enumerate(questions):
        if q.edge.question_type != "contributor":
            continue
        if any(p.detail for p in q.peers):
            continue
        owner_repo = _owner_repo_from_node_id(q.edge.source_id)
        if not owner_repo:
            continue
        needs.append((i, owner_repo[0], owner_repo[1]))

    if not needs:
        return questions

    unique_repos: Dict[str, Tuple[str, str]] = {}
    for _, owner, repo in needs:
        key = "{}/{}".format(owner, repo).lower()
        if key not in unique_repos:
            unique_repos[key] = (owner, repo)

    logger.info(
        "[JURY] Fetching live contributor stats for %d repos (%d questions)",
        len(unique_repos),
        len(needs),
    )

    repo_keys = list(unique_repos.keys())
    fetch_results = await asyncio.gather(
        *(
            fetch_contributor_stats(unique_repos[k][0], unique_repos[k][1])
            for k in repo_keys
        ),
        return_exceptions=True,
    )

    cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for key, result in zip(repo_keys, fetch_results):
        if isinstance(result, Exception) or not result:
            continue
        cache[key] = {
            str(s.get("login", "")).lower(): s for s in result
        }

    enriched = list(questions)
    for idx, owner, repo in needs:
        repo_key = "{}/{}".format(owner, repo).lower()
        stats_by_login = cache.get(repo_key)
        if not stats_by_login:
            continue

        q = enriched[idx]

        new_peers: List[JuryPeer] = []
        for peer in q.peers:
            stats = stats_by_login.get(peer.name.lower())
            if stats and not peer.detail:
                new_peers.append(
                    peer.model_copy(update={"detail": _detail_from_stats(stats)})
                )
            else:
                new_peers.append(peer)

        subject_stats = stats_by_login.get(q.subject_name.lower())
        new_summary = q.subject_summary
        if subject_stats and "commit" not in q.subject_summary:
            peer_rank = next(
                (i + 1 for i, p in enumerate(new_peers) if p.is_subject), 0
            )
            new_summary = _summary_from_stats(
                subject_stats,
                peer_rank,
                len(new_peers),
            )

        enriched[idx] = q.model_copy(
            update={"peers": new_peers, "subject_summary": new_summary}
        )

    return enriched


def _detail_from_stats(stats: Dict[str, Any]) -> str:
    parts: list[str] = []
    commits = stats.get("total_commits")
    if commits and int(commits) > 0:
        parts.append("{} commits".format(_fmt_int(commits)))
    lines = stats.get("total_lines")
    if lines and int(lines) > 0:
        parts.append("{} lines changed".format(_fmt_int(lines)))
    additions = stats.get("total_additions")
    deletions = stats.get("total_deletions")
    if additions is not None and deletions is not None:
        total = int(additions) + int(deletions)
        if total > 0:
            ratio = int(additions) / total
            if ratio > 0.75:
                parts.append("mostly new code")
            elif ratio < 0.3:
                parts.append("mostly refactoring")
    return " · ".join(parts)


def _summary_from_stats(
    stats: Dict[str, Any],
    peer_rank: int,
    total_peers: int,
) -> str:
    if peer_rank == 1 and total_peers > 1:
        rank_text = "The project's most active contributor"
    elif peer_rank == 2 and total_peers > 2:
        rank_text = "The second most active contributor"
    elif peer_rank <= 3 and total_peers > 3:
        rank_text = "One of the top contributors"
    elif total_peers > 1:
        rank_text = "Contributor #{} of {}".format(peer_rank, total_peers)
    else:
        rank_text = "A contributor to this project"

    work_bits: list[str] = []
    commits = stats.get("total_commits")
    if commits and int(commits) > 0:
        work_bits.append("with {} commits".format(_fmt_int(commits)))
    additions = stats.get("total_additions")
    deletions = stats.get("total_deletions")
    if additions is not None and deletions is not None:
        add_val, del_val = int(additions), int(deletions)
        total = add_val + del_val
        if total > 0:
            ratio = add_val / total
            if ratio > 0.8:
                work_bits.append("primarily writing new code and features")
            elif ratio < 0.3:
                work_bits.append("focused on refactoring and cleanup")
            else:
                work_bits.append("a mix of new features and maintenance")
    if work_bits:
        return "{}, {}.".format(rank_text, ", ".join(work_bits))
    return rank_text + "."


# ---------------------------------------------------------------------------
# Prior upsert — feed human corrections into future AI decisions
# ---------------------------------------------------------------------------


def _entity_type_from_nodes(
    source_node: Dict[str, Any],
    target_node: Dict[str, Any],
) -> str:
    target_type = str(target_node.get("type", ""))
    if target_type in ("CONTRIBUTOR", "AUTHOR"):
        return "contributor"
    source_type = str(source_node.get("type", ""))
    if source_type in ("PAPER", "CITED_WORK") and target_type == "CITED_WORK":
        return "citation"
    return "dependency"


async def _upsert_priors(
    session: Any,
    grouped_votes: Dict[Tuple[str, str], List[EdgeVote]],
    node_by_id: Dict[str, Dict[str, Any]],
) -> None:
    """Compute aggregated correction priors and upsert into jury_priors."""
    for (edge_source, edge_target), votes in grouped_votes.items():
        if not votes:
            continue

        target_node = node_by_id.get(edge_target, {})
        source_node = node_by_id.get(edge_source, {})
        entity_name = str(target_node.get("label", "")).strip()
        if not entity_name:
            continue

        entity_type = _entity_type_from_nodes(source_node, target_node)

        avg_human = sum(float(v.human_weight) for v in votes) / len(votes)
        avg_ai = sum(float(v.ai_weight) for v in votes) / len(votes)
        correction = avg_human / max(avg_ai, 0.01)

        existing = (
            (
                await session.execute(
                    select(JuryPrior).where(
                        JuryPrior.entity_name == entity_name,
                        JuryPrior.entity_type == entity_type,
                    )
                )
            )
            .scalars()
            .first()
        )

        if existing:
            n = existing.vote_count
            new_count = n + len(votes)
            existing.avg_human_weight = (
                existing.avg_human_weight * n + avg_human * len(votes)
            ) / new_count
            existing.avg_ai_weight = (
                existing.avg_ai_weight * n + avg_ai * len(votes)
            ) / new_count
            existing.avg_correction = existing.avg_human_weight / max(
                existing.avg_ai_weight, 0.01
            )
            existing.vote_count = new_count
        else:
            session.add(
                JuryPrior(
                    entity_name=entity_name,
                    entity_type=entity_type,
                    avg_human_weight=avg_human,
                    avg_ai_weight=avg_ai,
                    avg_correction=correction,
                    vote_count=len(votes),
                )
            )

    logger.info("[PRIORS] Upserted priors for %d entities", len(grouped_votes))


# ---------------------------------------------------------------------------
# POST /jury/answers
# ---------------------------------------------------------------------------


@router.post("/answers", response_model=SubmitJuryAnswersResponse)
async def submit_jury_answers(body: SubmitJuryAnswersRequest):
    accepted = 0
    updated_projects = 0
    reward_eth = 0.0

    touched_projects: Dict[int, Set[Tuple[str, str]]] = defaultdict(set)
    accepted_votes: Dict[int, List[EdgeVote]] = defaultdict(list)

    async with session_scope() as session:
        project_cache: Dict[int, Optional[Project]] = {}

        for answer in body.answers:
            project = project_cache.get(answer.project_id)
            if answer.project_id not in project_cache:
                project = await session.get(Project, answer.project_id)
                project_cache[answer.project_id] = project
            if not project:
                continue

            graph_data = project.graph_data or {}
            edges = graph_data.get("edges") or []
            edge = _find_edge(edges, answer.edge_source, answer.edge_target)
            if not edge:
                continue

            ai_weight = _clamp01(_safe_float(edge.get("weight"), 0.0))
            confidence = _clamp01(float(answer.confidence))
            human_weight = _clamp01(float(answer.human_percentage) / 100.0)

            vote = EdgeVote(
                project_id=project.id,
                wallet_address=body.wallet_address.strip().lower(),
                edge_source=answer.edge_source,
                edge_target=answer.edge_target,
                ai_weight=ai_weight,
                human_weight=human_weight,
                confidence=confidence,
                question_type="edge",
            )
            session.add(vote)
            accepted_votes[project.id].append(vote)

            accepted += 1
            reward_eth += 0.0001 * (0.5 + (confidence * 0.5))
            touched_projects[project.id].add((answer.edge_source, answer.edge_target))

        for project_id in touched_projects:
            project = project_cache.get(project_id)
            if not project:
                continue

            graph_data = project.graph_data or {}
            edges = graph_data.get("edges") or []
            if not edges:
                continue

            vote_rows = (
                (
                    await session.execute(
                        select(EdgeVote).where(EdgeVote.project_id == project_id)
                    )
                )
                .scalars()
                .all()
            )

            grouped_votes: Dict[Tuple[str, str], List[EdgeVote]] = defaultdict(list)
            for row in vote_rows:
                grouped_votes[(row.edge_source, row.edge_target)].append(row)

            touched_sources: Set[str] = set()
            for (edge_source, edge_target), votes in grouped_votes.items():
                edge = _find_edge(edges, edge_source, edge_target)
                if not edge or not votes:
                    continue

                source = edge.get("source")
                if source:
                    touched_sources.add(str(source))

                confidence_weights = [max(float(v.confidence), 0.1) for v in votes]
                total_conf = sum(confidence_weights) or 1.0
                human_mean = (
                    sum(
                        float(v.human_weight) * conf
                        for v, conf in zip(votes, confidence_weights)
                    )
                    / total_conf
                )

                ai_weight = _clamp01(_safe_float(edge.get("weight"), 0.0))
                vote_strength = min(1.0, len(votes) / 5.0)
                consensus = (1.0 - vote_strength) * ai_weight + (
                    vote_strength * human_mean
                )
                consensus = _clamp01(consensus)

                edge["weight"] = round(consensus, 4)
                edge["label"] = "{}%".format(round(consensus * 100, 1))
                edge_meta = edge.setdefault("metadata", {})
                edge_meta["jury"] = {
                    "votes": len(votes),
                    "human_mean": round(human_mean, 4),
                    "consensus_weight": round(consensus, 4),
                }

            for source_id in touched_sources:
                sibling_edges = [e for e in edges if e.get("source") == source_id]
                total_weight = sum(
                    _safe_float(e.get("weight"), 0.0) for e in sibling_edges
                )
                if total_weight <= 0:
                    continue
                for edge in sibling_edges:
                    new_weight = _safe_float(edge.get("weight"), 0.0) / total_weight
                    new_weight = _clamp01(new_weight)
                    edge["weight"] = round(new_weight, 4)
                    edge["label"] = "{}%".format(round(new_weight * 100, 1))

            attribution = _compute_leaf_attribution(graph_data)
            project.graph_data = graph_data
            project.attribution = attribution
            project.top_contributors = _top_contributors(attribution)
            updated_projects += 1

            node_by_id = _node_lookup(graph_data.get("nodes") or [])
            await _upsert_priors(session, grouped_votes, node_by_id)

            if settings.provenance_enabled and accepted_votes[project_id]:
                await session.flush()
                try:
                    from app.schemas.provenance import JuryEventV1
                    from app.services.provenance_store import append_events_snapshot

                    events = [
                        JuryEventV1(
                            sequence=1,
                            event_id="edge-vote:{}".format(vote.id),
                            edge_source=vote.edge_source,
                            edge_target=vote.edge_target,
                            prior_weight=float(vote.ai_weight),
                            human_weight=float(vote.human_weight),
                            confidence=float(vote.confidence),
                        )
                        for vote in accepted_votes[project_id]
                    ]
                    async with session.begin_nested():
                        await append_events_snapshot(session, project_id, events)
                except Exception as exc:
                    logger.warning(
                        "Provenance jury snapshot unavailable: error_type=%s",
                        type(exc).__name__,
                    )

    return SubmitJuryAnswersResponse(
        accepted=accepted,
        updated_projects=updated_projects,
        reward_eth=round(reward_eth, 6),
    )
