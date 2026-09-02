"""SSE streaming endpoint that runs a trace and streams log lines + graph data"""

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from app.config import settings
from app.database import session_scope
from app.models.project import Project
from app.services.citation_graph_builder import build_citation_graph
from app.services.github import parse_repo_owner_and_name
from app.services.graph_builder import build_contribution_graph
from app.services.package_graph_builder import build_package_graph
from app.services.screenshot import take_project_screenshot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["stream"])

LOG_PREFIXES = re.compile(r"\[(GRAPH|CIT|PKGGRAPH|PKG|REG|ARXIV|LLM)\]")
_SAVE_LOCKS: dict[str, asyncio.Lock] = {}


def _slugify(name: str) -> str:
    return re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", name.lower()))


class SSELogHandler(logging.Handler):
    """Captures log records and pushes formatted messages to an asyncio queue."""

    def __init__(self, queue: asyncio.Queue):
        super().__init__()
        self.queue = queue

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass


def _sse_event(event: str, data) -> str:
    payload = json.dumps(data) if not isinstance(data, str) else data
    return f"event: {event}\ndata: {payload}\n\n"


def _detect_type(url: str) -> str:
    url_lower = url.lower()
    if "github.com" in url_lower:
        return "repo"
    if "arxiv.org" in url_lower:
        return "paper"
    if "npmjs.com" in url_lower or "pypi.org" in url_lower:
        return "package"
    return "repo"


def _extract_name_from_url(url: str, trace_type: str) -> str:
    if trace_type == "repo":
        try:
            _, repo = parse_repo_owner_and_name(url)
            return repo
        except Exception:
            pass
    elif trace_type == "paper":
        return _extract_arxiv_id(url)
    elif trace_type == "package":
        try:
            _, package_name = _parse_package_identity(url)
            return package_name
        except Exception:
            pass
    return url.split("/")[-1] or "unknown"


def _extract_ecosystem(url: str) -> str:
    try:
        ecosystem, _ = _parse_package_identity(url)
        return ecosystem
    except Exception:
        if "pypi.org" in url.lower():
            return "PYPI"
        return "NPM"


def _extract_arxiv_id(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]

    raw = parts[-1] if parts else path
    if raw.endswith(".pdf"):
        raw = raw[:-4]

    m_new = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", raw, flags=re.IGNORECASE)
    if m_new:
        return m_new.group(1)

    m_old = re.search(r"([a-z\-\.]+/\d{7})(v\d+)?", raw, flags=re.IGNORECASE)
    if m_old:
        return m_old.group(1).lower()

    return raw


def _normalize_pypi_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _parse_package_identity(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [unquote(p) for p in parsed.path.split("/") if p]

    if "pypi.org" in host:
        if len(parts) >= 2 and parts[0].lower() == "project":
            name = parts[1]
        elif parts:
            name = parts[0]
        else:
            raise ValueError("Could not parse PyPI package name from URL")
        return "PYPI", _normalize_pypi_name(name)

    if parts and parts[0].lower() == "package":
        parts = parts[1:]

    if not parts:
        raise ValueError("Could not parse npm package name from URL")

    if parts[0].startswith("@") and len(parts) >= 2:
        name = "{}/{}".format(parts[0], parts[1])
    else:
        name = parts[0]
    return "NPM", name.lower()


def _canonical_source_url(url: str, trace_type: str) -> str:
    if trace_type == "repo":
        owner, repo = parse_repo_owner_and_name(url)
        return "https://github.com/{}/{}".format(owner.lower(), repo.lower())

    if trace_type == "paper":
        return "https://arxiv.org/abs/{}".format(_extract_arxiv_id(url))

    if trace_type == "package":
        ecosystem, package_name = _parse_package_identity(url)
        if ecosystem == "PYPI":
            return "https://pypi.org/project/{}/".format(package_name)
        return "https://www.npmjs.com/package/{}".format(package_name)

    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    host = (parsed.netloc or "").lower()
    path = parsed.path.rstrip("/")
    return "{}://{}{}".format(scheme, host, path)


def _canonical_source_key(url: str, trace_type: str) -> str:
    canonical = _canonical_source_url(url, trace_type)
    return "{}:{}".format(trace_type, canonical.rstrip("/").lower())


def _project_to_dict(project: Project) -> dict:
    return {
        "id": project.id,
        "slug": project.slug,
        "name": project.name,
        "category": project.category,
        "type": project.type,
        "summary": project.summary,
        "description": project.description,
        "source_url": project.source_url,
        "raised": project.raised,
        "contributors": project.contributors,
        "depth": project.depth,
        "graph_data": project.graph_data,
        "attribution": project.attribution,
        "dependencies": project.dependencies,
        "top_contributors": project.top_contributors,
        "cover_image_url": project.cover_image_url,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


def _apply_payload(project: Project, result: dict, canonical_url: str) -> None:
    project.name = result["name"]
    project.category = result["category"]
    project.type = result["type"]
    project.summary = result["summary"]
    project.description = result["description"]
    project.source_url = canonical_url
    project.contributors = result["contributors"]
    project.depth = result["depth"]
    project.graph_data = result["graph_data"]
    project.attribution = result["attribution"]
    project.dependencies = result["dependencies"]
    project.top_contributors = result["top_contributors"]


async def _store_provenance_sidecar(session, project: Project, result: dict) -> None:
    """Best-effort, default-off sidecar; never feeds back into attribution."""
    payload = result.get("_provenance")
    if not settings.provenance_enabled or not isinstance(payload, dict):
        return
    try:
        from app.services.provenance import build_manifest, public_identity_decisions
        from app.schemas.provenance import (
            EvidenceReferenceV1,
            HumanPriorInputV1,
            InferenceEventV1,
        )
        from app.services.provenance_store import save_snapshot

        lockfiles = payload["lockfiles"]
        warnings = [
            "mailmap_unavailable_for_remote_trace",
            "path_classification_uses_honeyflow_heuristics",
            "source_requests_guarded_but_not_ref_pinned",
        ]
        if not lockfiles:
            warnings.append("no_lockfiles_observed")
        if payload["paths_truncated"]:
            warnings.append("path_inventory_truncated")
        if payload["lockfiles_truncated"]:
            warnings.append("lockfile_inventory_truncated")
        warnings.extend(payload.get("capture_warnings", ()))
        evidence = [
            EvidenceReferenceV1(
                reference_id="lockfile:{}".format(
                    hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]
                ),
                path=path,
                change_kind="unknown",
            )
            for path in lockfiles
        ]
        manifest = build_manifest(
            repository_url=result["source_url"],
            source_commit_sha=payload["source_commit_sha"],
            attribution=result["attribution"],
            graph=result["graph_data"],
            lockfile_digests=lockfiles,
            attribution_config=payload["attribution_config"],
            identities=public_identity_decisions(result["attribution"].keys()),
            paths=payload["paths"],
            evidence=evidence,
            model=None,
            inference_events=tuple(
                InferenceEventV1.model_validate(item)
                for item in payload["inference_events"]
            ),
            human_prior_inputs=tuple(
                HumanPriorInputV1.model_validate(item)
                for item in payload["human_prior_inputs"]
            ),
            warnings=warnings,
        )
        async with session.begin_nested():
            await save_snapshot(session, project.id, manifest)
        logger.info("Provenance snapshot stored: project_id=%d", project.id)
    except Exception as exc:
        # Attribution persistence remains the source of truth; do not leak the
        # manifest payload or upstream response through logs.
        logger.warning("Provenance snapshot unavailable: error_type=%s", type(exc).__name__)


async def _upsert_project(session, result: dict, allow_create: bool) -> Project:
    trace_type = result["type"]
    canonical_url = _canonical_source_url(result["source_url"], trace_type)
    canonical_key = _canonical_source_key(result["source_url"], trace_type)

    # Defer large JSONB columns, we only overwrite them, never read them
    _jsonb_deferred = (
        defer(Project.graph_data),
        defer(Project.attribution),
        defer(Project.dependencies),
        defer(Project.top_contributors),
        defer(Project.cover_image_data),
    )

    existing = await session.scalar(
        select(Project)
        .where(Project.canonical_key == canonical_key)
        .options(*_jsonb_deferred)
    )
    if existing is None:
        # Backward-compatible fallback for rows created before canonical_key existed.
        existing = await session.scalar(
            select(Project)
            .where(
                Project.type == trace_type,
                Project.source_url == canonical_url,
            )
            .options(*_jsonb_deferred)
        )

    if existing:
        _apply_payload(existing, result, canonical_url)
        existing.canonical_key = canonical_key
        # Set updated_at explicitly so the Python object has a real datetime
        # after flush (onupdate=func.now() sends now() to DB but leaves the
        # Python attr stale in async mode without eager_defaults)
        existing.updated_at = datetime.now(timezone.utc)
        await session.flush()
        return existing

    if not allow_create:
        raise RuntimeError("Expected existing project after unique race fallback")

    slug = _slugify(result["name"]) or "project"
    base_slug = slug
    suffix = 1
    while (
        await session.scalar(select(Project.id).where(Project.slug == slug)) is not None
    ):
        suffix += 1
        slug = "{}-{}".format(base_slug, suffix)

    project = Project(
        slug=slug,
        canonical_key=canonical_key,
        name=result["name"],
        category=result["category"],
        type=result["type"],
        summary=result["summary"],
        description=result["description"],
        source_url=canonical_url,
        raised=0.0,
        contributors=result["contributors"],
        depth=result["depth"],
        graph_data=result["graph_data"],
        attribution=result["attribution"],
        dependencies=result["dependencies"],
        top_contributors=result["top_contributors"],
    )
    session.add(project)
    await session.flush()
    return project


async def _save_project(result: dict) -> dict:
    """Save trace result to DB with retry logic.

    Each attempt opens a fresh session (NullPool = fresh connection from
    PgBouncer every time), so retries won't hit a poisoned connection.
    No outer timeout — each attempt is bounded by SQLAlchemy / Neon limits
    """
    canonical_key = _canonical_source_key(result["source_url"], result["type"])
    lock = _SAVE_LOCKS.setdefault(canonical_key, asyncio.Lock())

    input_nodes = len(result.get("graph_data", {}).get("nodes", []))
    input_edges = len(result.get("graph_data", {}).get("edges", []))
    logger.info(
        "DB save starting: %s — %d nodes/%d edges",
        result.get("name"),
        input_nodes,
        input_edges,
    )

    last_exc: Exception | None = None
    for attempt in range(3):
        async with lock:
            try:
                async with session_scope() as session:
                    project = await _upsert_project(session, result, allow_create=True)
                    await _store_provenance_sidecar(session, project, result)
                    # No refresh() — expire_on_commit=False keeps all attrs;
                    # INSERT gets id/timestamps via RETURNING; UPDATE path
                    # sets updated_at explicitly in _upsert_project
                    saved = _project_to_dict(project)
                    saved_nodes = len(saved.get("graph_data", {}).get("nodes", []))
                    logger.info(
                        "DB save OK: %s — input %d nodes/%d edges, saved %d nodes",
                        result.get("name"),
                        input_nodes,
                        input_edges,
                        saved_nodes,
                    )
                    return saved
            except IntegrityError:
                try:
                    async with session_scope() as session:
                        project = await _upsert_project(
                            session, result, allow_create=False
                        )
                        return _project_to_dict(project)
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "DB save (integrity retry) attempt %d failed: %s [%s]",
                        attempt + 1,
                        type(exc).__name__,
                        exc,
                    )
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "DB save attempt %d failed: %s [%s]",
                    attempt + 1,
                    type(exc).__name__,
                    exc,
                )
        if attempt < 2:
            await asyncio.sleep(2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


async def _apply_cover_image(
    screenshot_task: asyncio.Task,
    actual_slug: str,
) -> str | None:
    """Await a screenshot task, persist image bytes to DB, update URL.

    Returns the cover_url on success, None on failure.
    """
    try:
        png_bytes = await asyncio.wait_for(screenshot_task, timeout=60.0)
        if not png_bytes:
            return None

        cover_url = f"/projects/{actual_slug}/cover"

        async with session_scope() as session:
            project = await session.scalar(
                select(Project)
                .where(Project.slug == actual_slug)
                .options(
                    defer(Project.graph_data),
                    defer(Project.attribution),
                    defer(Project.dependencies),
                    defer(Project.top_contributors),
                )
            )
            if project:
                project.cover_image_data = png_bytes
                project.cover_image_url = cover_url
                logger.info("Cover image saved to DB for %s", actual_slug)
        return cover_url
    except Exception as exc:
        logger.warning("Cover image finalization failed for %s: %s", actual_slug, exc)
        return None


async def _run_trace(
    url: str,
    trace_type: str,
    log_queue: asyncio.Queue,
    max_depth: Optional[int] = None,
    max_children: Optional[int] = None,
):
    """Run the appropriate builder and return a normalized result payload."""
    handler = SSELogHandler(log_queue)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))

    root_logger = logging.getLogger("app")
    root_logger.addHandler(handler)

    try:
        if trace_type == "repo":
            materials = None
            if settings.provenance_enabled:
                try:
                    from app.services.github import fetch_provenance_materials

                    owner, repo = parse_repo_owner_and_name(url)
                    materials = await fetch_provenance_materials(owner, repo)
                except Exception as exc:
                    logger.warning(
                        "Provenance source material unavailable: error_type=%s",
                        type(exc).__name__,
                    )
            attribution_capture = None
            if settings.provenance_enabled:
                from app.services.attribution_context import capture_attribution_inputs

                with capture_attribution_inputs() as attribution_capture:
                    graph, config, attribution = await build_contribution_graph(
                        url,
                        max_depth=max_depth,
                        max_children=max_children,
                    )
            else:
                graph, config, attribution = await build_contribution_graph(
                    url, max_depth=max_depth, max_children=max_children
                )
            graph_dict = graph.model_dump()
            name = _extract_name_from_url(url, trace_type)

            if materials is not None:
                try:
                    from app.services.github import (
                        fetch_repo_head_sha,
                        require_public_repository,
                    )

                    owner, repo = parse_repo_owner_and_name(url)
                    await require_public_repository(owner, repo)
                    if await fetch_repo_head_sha(owner, repo) != materials[0]:
                        logger.warning("Provenance source changed during trace; snapshot skipped")
                        materials = None
                except Exception as exc:
                    logger.warning(
                        "Provenance source recheck unavailable: error_type=%s",
                        type(exc).__name__,
                    )
                    materials = None

            sorted_contribs = sorted(
                attribution.items(), key=lambda x: x[1], reverse=True
            )[:10]
            total = sum(attribution.values()) or 1
            top_contributors = [
                {"name": c[0], "percentage": f"{(c[1] / total) * 100:.1f}%"}
                for c in sorted_contribs
            ]
            deps = list(
                dict.fromkeys(
                    n["label"]
                    for n in graph_dict["nodes"]
                    if n["type"] in ("PACKAGE", "BODY_OF_WORK")
                )
            )[:20]

            result = {
                "name": name,
                "category": "Infrastructure",
                "type": "repo",
                "summary": f"Contribution graph for {name}",
                "description": f"Automatically traced dependency and contribution graph for the {name} repository.",
                "source_url": url,
                "contributors": len(attribution),
                "depth": config.max_depth,
                "graph_data": graph_dict,
                "attribution": attribution,
                "dependencies": deps,
                "top_contributors": top_contributors,
            }
            if materials is not None:
                result["_provenance"] = {
                    "source_commit_sha": materials[0],
                    "lockfiles": materials[1],
                    "paths": materials[2],
                    "paths_truncated": materials[3],
                    "lockfiles_truncated": materials[4],
                    "attribution_config": {
                        "effective_request": config.model_dump(mode="json"),
                        "graph_settings": settings.graph.model_dump(mode="json"),
                    },
                    **(
                        attribution_capture.snapshot()
                        if attribution_capture is not None
                        else {
                            "inference_events": [],
                            "human_prior_inputs": [],
                            "capture_warnings": [],
                        }
                    ),
                }
            return result

        if trace_type == "paper":
            arxiv_id = _extract_arxiv_id(url)
            graph, config, attribution, title = await build_citation_graph(arxiv_id)
            graph_dict = graph.model_dump()
            name = title or arxiv_id

            sorted_contribs = sorted(
                attribution.items(), key=lambda x: x[1], reverse=True
            )[:10]
            total = sum(attribution.values()) or 1
            top_contributors = [
                {"name": c[0], "percentage": f"{(c[1] / total) * 100:.1f}%"}
                for c in sorted_contribs
            ]
            deps = [
                n["label"] for n in graph_dict["nodes"] if n["type"] in ("CITED_WORK",)
            ][:20]

            return {
                "name": name,
                "category": "Research",
                "type": "paper",
                "summary": f"Citation graph for {name}",
                "description": f"Automatically traced citation and author attribution graph for the paper: {name}.",
                "source_url": url,
                "contributors": len(attribution),
                "depth": config.max_depth,
                "graph_data": graph_dict,
                "attribution": attribution,
                "dependencies": deps,
                "top_contributors": top_contributors,
            }

        if trace_type == "package":
            pkg_name = _extract_name_from_url(url, trace_type)
            ecosystem = _extract_ecosystem(url)
            graph, config, attribution = await build_package_graph(pkg_name, ecosystem)
            graph_dict = graph.model_dump()

            sorted_contribs = sorted(
                attribution.items(), key=lambda x: x[1], reverse=True
            )[:10]
            total = sum(attribution.values()) or 1
            top_contributors = [
                {"name": c[0], "percentage": f"{(c[1] / total) * 100:.1f}%"}
                for c in sorted_contribs
            ]
            deps = list(
                dict.fromkeys(
                    n["label"]
                    for n in graph_dict["nodes"]
                    if n["type"] in ("PACKAGE", "BODY_OF_WORK")
                )
            )[:20]

            return {
                "name": pkg_name,
                "category": "Infrastructure",
                "type": "package",
                "summary": f"Package dependency graph for {pkg_name}",
                "description": f"Automatically traced dependency and contribution graph for the {pkg_name} package.",
                "source_url": url,
                "contributors": len(attribution),
                "depth": config.max_depth,
                "graph_data": graph_dict,
                "attribution": attribution,
                "dependencies": deps,
                "top_contributors": top_contributors,
            }

        raise ValueError(f"Unknown trace type: {trace_type}")
    finally:
        root_logger.removeHandler(handler)


@router.get("/trace")
async def stream_trace(
    url: str = Query(..., description="URL to trace (GitHub, arXiv, npm, PyPI)"),
    type: Optional[str] = Query(None, description="Force type: repo, paper, package"),
    depth: Optional[int] = Query(None, ge=1, le=10, description="Max recursion depth"),
    max_children: Optional[int] = Query(
        None, ge=1, le=50, description="Max child nodes per level"
    ),
):
    trace_type = type or _detect_type(url)
    logger.info(
        "[STREAM] trace called: url=%s type=%s depth=%s max_children=%s",
        url,
        trace_type,
        depth,
        max_children,
    )
    log_queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    async def event_generator():
        trace_task: Optional[asyncio.Task] = None
        screenshot_task: Optional[asyncio.Task] = None
        try:
            # Start screenshot immediately, in parallel with the trace.
            # This way it runs during the graph-building work instead of after
            canonical_url = _canonical_source_url(url, trace_type)
            screenshot_task = asyncio.create_task(
                take_project_screenshot(canonical_url)
            )

            yield _sse_event("log", f"Starting {trace_type} trace for {url}...")

            trace_task = asyncio.create_task(
                _run_trace(
                    url,
                    trace_type,
                    log_queue,
                    max_depth=depth,
                    max_children=max_children,
                )
            )

            while not trace_task.done():
                try:
                    msg = await asyncio.wait_for(log_queue.get(), timeout=0.3)
                    yield _sse_event("log", msg)
                except asyncio.TimeoutError:
                    pass

            while not log_queue.empty():
                msg = log_queue.get_nowait()
                yield _sse_event("log", msg)

            result = trace_task.result()
            yield _sse_event("graph", result["graph_data"])

            slug = _slugify(result["name"])
            try:
                project_dict = await _save_project(result)
                actual_slug = project_dict.get("slug", slug)

                # Check if screenshot finished during the trace
                if screenshot_task.done():
                    try:
                        cover_url = await _apply_cover_image(
                            screenshot_task, actual_slug,
                        )
                        if cover_url:
                            project_dict["cover_image_url"] = cover_url
                    except Exception as exc:
                        logger.warning("Screenshot inline apply failed: %s", exc)
                else:
                    # Still running — finalize in background after it completes
                    asyncio.create_task(
                        _apply_cover_image(screenshot_task, actual_slug)
                    )

                yield _sse_event("result", project_dict)

            except Exception as save_exc:
                if screenshot_task and not screenshot_task.done():
                    screenshot_task.cancel()
                logger.warning(
                    "DB save failed (%s), sending fallback result: %s",
                    type(save_exc).__name__,
                    save_exc,
                )
                yield _sse_event(
                    "result",
                    {
                        "id": 0,
                        "slug": slug,
                        "name": result["name"],
                        "category": result["category"],
                        "type": result["type"],
                        "summary": result["summary"],
                        "description": result["description"],
                        "source_url": result["source_url"],
                        "raised": 0,
                        "contributors": result["contributors"],
                        "depth": result["depth"],
                        "graph_data": result["graph_data"],
                        "attribution": result["attribution"],
                        "dependencies": result["dependencies"],
                        "top_contributors": result["top_contributors"],
                        "cover_image_url": None,
                        "created_at": "",
                        "updated_at": "",
                    },
                )

        except asyncio.CancelledError:
            if trace_task and not trace_task.done():
                trace_task.cancel()
            if screenshot_task and not screenshot_task.done():
                screenshot_task.cancel()
            logger.info("SSE client disconnected, trace cancelled")
            return
        except Exception as exc:
            if screenshot_task and not screenshot_task.done():
                screenshot_task.cancel()
            yield _sse_event("error", str(exc))

        yield _sse_event("done", "complete")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
