"""GitHub API client.

Handles contributor fetching, repo metadata, README, Git Trees API,
and detailed contributor stats.
"""

import asyncio
import base64
import hashlib
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.schemas.contributions import Contributor

logger = logging.getLogger(__name__)


_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Limit concurrent GitHub API requests to avoid ConnectTimeout errors
_SEMAPHORE = asyncio.Semaphore(15)
_CLIENT: Optional[httpx.AsyncClient] = None

# Simple TTL cache: key -> (value, expiry_timestamp)
_github_cache: Dict[str, Tuple[Any, float]] = {}
_CACHE_TTL = 600  # 10 minutes


def _cache_get(key: str) -> Optional[Any]:
    entry = _github_cache.get(key)
    if entry is None:
        return None
    value, expiry = entry
    if time.monotonic() > expiry:
        del _github_cache[key]
        return None
    return value


def _cache_set(key: str, value: Any, ttl: Optional[float] = None) -> None:
    _github_cache[key] = (value, time.monotonic() + (ttl or _CACHE_TTL))


def _get_client() -> httpx.AsyncClient:
    """Get or create a shared httpx client with connection pooling."""
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _CLIENT


def _headers() -> Dict[str, str]:
    h: Dict[str, str] = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        h["Authorization"] = "Bearer {}".format(settings.github_token)
    return h


def parse_repo_owner_and_name(repo_url: str) -> Tuple[str, str]:
    """Extract owner and repo name from a GitHub URL.

    Supports (and normalizes to owner/repo):
        https://github.com/owner/repo
        https://github.com/owner/repo.git
        https://github.com/owner/repo/
        https://github.com/owner/repo/tree/main
        https://github.com/owner/repo/issues/123
    """
    parsed = urlparse(str(repo_url))

    if parsed.hostname not in ("github.com", "www.github.com"):
        raise ValueError("Not a GitHub URL: {}".format(repo_url))

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("Could not parse owner/repo from URL: {}".format(repo_url))
    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        raise ValueError("Could not parse owner/repo from URL: {}".format(repo_url))
    return owner, repo


# ------------------------------------------------------------------
# Basic contributor list (used by /trace_contributions)
# ------------------------------------------------------------------


async def fetch_top_contributors(
    repo_url: str,
    *,
    limit: int = 10,
) -> List[Contributor]:
    """Fetch the top contributors for a GitHub repository."""
    owner, repo = parse_repo_owner_and_name(repo_url)

    url = "{}/repos/{}/{}/contributors".format(settings.github_api_base, owner, repo)
    params: Dict[str, Any] = {"per_page": limit * 2, "anon": "false"}

    logger.info("GET  %s/%s contributors (limit=%d)", owner, repo, limit)
    async with _SEMAPHORE:
        client = _get_client()
        response = await client.get(url, headers=_headers(), params=params)
        response.raise_for_status()

    data: List[Dict] = response.json()

    return [
        Contributor(username=c["login"], contributions=c["contributions"])
        for c in data
        if "[bot]" not in c["login"]
    ][:limit]


# ------------------------------------------------------------------
# Repo metadata
# ------------------------------------------------------------------


async def fetch_repo_metadata(owner: str, repo: str) -> Dict[str, Any]:
    """Fetch basic repo info and language breakdown."""
    logger.info("GET  %s/%s metadata + languages", owner, repo)
    base = settings.github_api_base
    async with _SEMAPHORE:
        client = _get_client()
        repo_resp = await client.get(
            "{}/repos/{}/{}".format(base, owner, repo), headers=_headers()
        )
        repo_resp.raise_for_status()
        repo_data = repo_resp.json()

        lang_resp = await client.get(
            "{}/repos/{}/{}/languages".format(base, owner, repo), headers=_headers()
        )
        languages = lang_resp.json() if lang_resp.status_code == 200 else {}

    logger.info(
        "     %s/%s -> %s, %d languages",
        owner,
        repo,
        (repo_data.get("description") or "")[:60],
        len(languages),
    )
    return {
        "description": repo_data.get("description") or "",
        "default_branch": repo_data.get("default_branch", "main"),
        "size_kb": repo_data.get("size", 0),
        "languages": languages,
        "html_url": repo_data.get("html_url", ""),
    }


async def fetch_readme(owner: str, repo: str, max_chars: int = 4000) -> str:
    """Fetch raw README content, truncated for LLM context."""
    logger.info("GET  %s/%s README", owner, repo)
    url = "{}/repos/{}/{}/readme".format(settings.github_api_base, owner, repo)
    headers = {**_headers(), "Accept": "application/vnd.github.raw+json"}

    async with _SEMAPHORE:
        client = _get_client()
        resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            logger.info("     %s/%s README not found", owner, repo)
            return ""
        resp.raise_for_status()

    text = resp.text[:max_chars]
    logger.info("     %s/%s README fetched (%d chars)", owner, repo, len(text))
    return text


# ------------------------------------------------------------------
# Git Trees API (replaces tarball download)
# ------------------------------------------------------------------


async def fetch_repo_tree(owner: str, repo: str, branch: str = "HEAD") -> List[Dict[str, Any]]:
    """Fetch the full recursive file tree via the Git Trees API.

    Returns a list of tree entries, each with keys: path, mode, type, size, sha.
    """
    logger.info("GET  %s/%s git/trees/%s?recursive=1", owner, repo, branch)
    url = "{}/repos/{}/{}/git/trees/{}".format(
        settings.github_api_base, owner, repo, branch
    )
    async with _SEMAPHORE:
        client = _get_client()
        resp = await client.get(url, headers=_headers(), params={"recursive": "1"})
        resp.raise_for_status()

    data = resp.json()
    tree = data.get("tree", [])
    truncated = data.get("truncated", False)
    logger.info(
        "     %s/%s tree: %d entries (truncated=%s)", owner, repo, len(tree), truncated
    )
    return tree


_LOCKFILE_NAMES = {
    "bun.lock",
    "bun.lockb",
    "cargo.lock",
    "composer.lock",
    "gemfile.lock",
    "go.sum",
    "package-lock.json",
    "pipfile.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
}


async def fetch_repo_head_sha(owner: str, repo: str) -> str:
    """Resolve the current default-branch HEAD to an exact commit SHA."""
    url = "{}/repos/{}/{}/commits/HEAD".format(settings.github_api_base, owner, repo)
    async with _SEMAPHORE:
        response = await _get_client().get(url, headers=_headers())
        response.raise_for_status()
    sha = response.json().get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("GitHub returned an invalid source commit SHA")
    return sha


async def require_public_repository(owner: str, repo: str) -> None:
    """Fail closed unless GitHub currently identifies the repository as public."""
    url = "{}/repos/{}/{}".format(settings.github_api_base, owner, repo)
    async with _SEMAPHORE:
        response = await _get_client().get(url, headers=_headers())
        response.raise_for_status()
    if response.json().get("private") is not False:
        raise ValueError("public provenance export is disabled for non-public repositories")


async def fetch_provenance_materials(
    owner: str, repo: str
) -> tuple[str, Dict[str, str], List[str], bool, bool]:
    """Collect bounded lockfile SHA-256 digests at an exact public commit."""
    base = settings.github_api_base
    await require_public_repository(owner, repo)

    sha = await fetch_repo_head_sha(owner, repo)
    async with _SEMAPHORE:
        tree_response = await _get_client().get(
            "{}/repos/{}/{}/git/trees/{}".format(base, owner, repo, sha),
            headers=_headers(),
            params={"recursive": "1"},
        )
        tree_response.raise_for_status()
    tree_data = tree_response.json()
    if tree_data.get("truncated"):
        raise ValueError("GitHub returned a truncated repository tree")
    tree = tree_data.get("tree", [])
    public_paths = sorted(
        {
            entry["path"]
            for entry in tree
            if isinstance(entry, dict)
            and entry.get("type") == "blob"
            and isinstance(entry.get("path"), str)
            and 0 < len(entry["path"]) <= 512
            and "\x00" not in entry["path"]
        }
    )
    paths_truncated = len(public_paths) > 5_000
    public_paths = public_paths[:5_000]
    candidates = [
        entry
        for entry in tree
        if isinstance(entry, dict)
        and entry.get("type") == "blob"
        and isinstance(entry.get("path"), str)
        and entry["path"].split("/")[-1].casefold() in _LOCKFILE_NAMES
        and isinstance(entry.get("size"), int)
        and 0 <= entry["size"] <= 2_000_000
        and isinstance(entry.get("sha"), str)
    ]
    candidates = sorted(candidates, key=lambda item: item["path"])
    lockfiles_truncated = len(candidates) > 25
    selected = []
    selected_bytes = 0
    for entry in candidates[:25]:
        if selected_bytes + entry["size"] > 10_000_000:
            lockfiles_truncated = True
            break
        selected.append(entry)
        selected_bytes += entry["size"]
    required_paths = {entry["path"] for entry in selected}
    if not required_paths.issubset(public_paths):
        other_paths = [path for path in public_paths if path not in required_paths]
        public_paths = sorted(required_paths) + other_paths[: 5_000 - len(required_paths)]
        public_paths = sorted(public_paths)

    async def fetch_lockfile(entry: Dict[str, Any]) -> tuple[str, str]:
        async with _SEMAPHORE:
            blob_response = await _get_client().get(
                "{}/repos/{}/{}/git/blobs/{}".format(base, owner, repo, entry["sha"]),
                headers=_headers(),
            )
            blob_response.raise_for_status()
        blob = blob_response.json()
        if blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
            raise ValueError("GitHub returned an unsupported lockfile encoding")
        encoded = re.sub(r"\s+", "", blob["content"])
        content = base64.b64decode(encoded, validate=True)
        if len(content) != entry["size"]:
            raise ValueError("GitHub lockfile size changed during collection")
        return entry["path"], hashlib.sha256(content).hexdigest()

    lockfiles = await asyncio.gather(
        *(fetch_lockfile(entry) for entry in selected)
    )
    return (
        sha,
        dict(lockfiles),
        public_paths,
        paths_truncated,
        lockfiles_truncated,
    )


async def fetch_manifests_from_tree(
    owner: str, repo: str, tree: List[Dict[str, Any]]
) -> List[tuple]:
    """Find manifest files in the tree and fetch their contents.

    Returns list of (filename, content_text, ecosystem) tuples.
    """
    from app.services.parsers.manifest import MANIFEST_FILES

    root_manifests = []
    for entry in tree:
        if entry.get("type") != "blob":
            continue
        path = entry.get("path", "")
        # Only root-level manifests (no slashes in path)
        if "/" not in path and path in MANIFEST_FILES:
            root_manifests.append((path, MANIFEST_FILES[path]))

    results = []
    for filename, ecosystem in root_manifests:
        text = await fetch_file_content(owner, repo, filename)
        if text:
            results.append((filename, text, ecosystem))

    logger.info(
        "     %s/%s fetched %d manifest files via Contents API",
        owner, repo, len(results),
    )
    return results


def build_file_tree_from_api(tree: List[Dict[str, Any]], repo_name: str, max_depth: int = 2) -> str:
    """Build a compact file tree string from Git Trees API response."""
    skip = {
        "node_modules", ".git", "__pycache__", ".venv", "venv",
        "target", "dist", "build",
    }

    # Build a nested dict from flat paths
    root: Dict[str, Any] = {}
    for entry in tree:
        path = entry.get("path", "")
        parts = path.split("/")
        # Skip entries inside skipped directories
        if any(p in skip for p in parts):
            continue
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if entry.get("type") == "tree":
            node.setdefault(parts[-1], {})
        else:
            node[parts[-1]] = None  # leaf file

    lines: List[str] = []

    def _walk(subtree: Dict, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        dirs = sorted(k for k, v in subtree.items() if isinstance(v, dict))
        files = sorted(k for k, v in subtree.items() if v is None)
        for f in files:
            lines.append("{}{}".format(prefix, f))
        for d in dirs:
            lines.append("{}{}/".format(prefix, d))
            _walk(subtree[d], prefix + "  ", depth + 1)

    lines.append("{}/".format(repo_name))
    _walk(root, "  ", 0)
    return "\n".join(lines[:200])


def count_source_files_from_tree(tree: List[Dict[str, Any]]) -> int:
    """Count source files from Git Trees API response."""
    extensions = (".py", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".java", ".rb")
    skip = {"node_modules", ".git", "__pycache__", "venv", ".venv", "target"}
    count = 0
    for entry in tree:
        if entry.get("type") != "blob":
            continue
        path = entry.get("path", "")
        parts = path.split("/")
        if any(p in skip for p in parts):
            continue
        if any(path.endswith(ext) for ext in extensions):
            count += 1
    return count


# ------------------------------------------------------------------
# Detailed contributor stats (with lines changed)
# ------------------------------------------------------------------


async def fetch_contributor_stats(owner: str, repo: str) -> List[Dict[str, Any]]:
    """Fetch per-author contribution stats.

    Tries the detailed /stats/contributors endpoint first (gives lines changed).
    Falls back to the simpler /contributors endpoint (commit counts only) if
    the stats endpoint keeps returning 202.
    """
    cache_key = "stats:{}/{}".format(owner.lower(), repo.lower())
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info("GET  %s/%s stats/contributors (cache hit)", owner, repo)
        return cached

    # If GitHub was recently computing stats for this repo, skip retries
    skip_retries = _cache_get("computing:" + cache_key) is not None

    stats_url = "{}/repos/{}/{}/stats/contributors".format(
        settings.github_api_base, owner, repo
    )

    logger.info("GET  %s/%s stats/contributors (detailed)", owner, repo)
    async with _SEMAPHORE:
        client = _get_client()
        resp = await client.get(stats_url, headers=_headers())

        if not skip_retries:
            retries = 3
            backoff = 1.5
            while resp.status_code == 202 and retries > 0:
                logger.info(
                    "     %s/%s GitHub is computing stats, waiting %.1fs... (%d/3)",
                    owner,
                    repo,
                    backoff,
                    4 - retries,
                )
                await asyncio.sleep(backoff)
                resp = await client.get(stats_url, headers=_headers())
                retries -= 1
                backoff *= 1.5

        if resp.status_code == 200:
            raw: List[Dict] = resp.json()
            if isinstance(raw, list) and raw:
                logger.info(
                    "     %s/%s got detailed stats for %d contributors",
                    owner,
                    repo,
                    len(raw),
                )
                result = _parse_detailed_stats(raw)
                _cache_set(cache_key, result)
                return result

        if resp.status_code == 202:
            _cache_set("computing:" + cache_key, True, ttl=30)

        logger.info(
            "     %s/%s detailed stats not ready yet, using basic contributor list instead",
            owner,
            repo,
        )
        fallback_url = "{}/repos/{}/{}/contributors".format(
            settings.github_api_base, owner, repo
        )
        resp = await client.get(
            fallback_url, headers=_headers(), params={"per_page": 30}
        )
        if resp.status_code != 200:
            logger.warning(
                "     %s/%s contributors fallback also failed (%d)",
                owner,
                repo,
                resp.status_code,
            )
            return []

    data: List[Dict] = resp.json()
    result = [
        {
            "login": c["login"],
            "avatar_url": c.get("avatar_url", ""),
            "total_commits": c.get("contributions", 0),
            "total_additions": 0,
            "total_deletions": 0,
            "total_lines": c.get("contributions", 0),
        }
        for c in data
        if "[bot]" not in c.get("login", "")
    ]
    _cache_set(cache_key, result)
    return result


def _parse_detailed_stats(raw: List[Dict]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for entry in raw:
        author = entry.get("author")
        if not author or "[bot]" in author.get("login", ""):
            continue
        weeks = entry.get("weeks", [])
        total_add = sum(w.get("a", 0) for w in weeks)
        total_del = sum(w.get("d", 0) for w in weeks)
        total_commits = sum(w.get("c", 0) for w in weeks)
        results.append(
            {
                "login": author["login"],
                "avatar_url": author.get("avatar_url", ""),
                "total_commits": total_commits,
                "total_additions": total_add,
                "total_deletions": total_del,
                "total_lines": total_add + total_del,
            }
        )
    results.sort(key=lambda x: x["total_lines"], reverse=True)
    return results


# ------------------------------------------------------------------
# Fetch a single file via Contents API (for dep manifests at depth > 0)
# ------------------------------------------------------------------


async def fetch_contributor_commits(
    owner: str,
    repo: str,
    login: str,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Fetch recent commits by a contributor, with code patches for the top commit."""
    cache_key = "commits:{}/{}/{}:{}".format(
        owner.lower(), repo.lower(), login.lower(), limit
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info(
            "GET  %s/%s commits?author=%s (cache hit)", owner, repo, login
        )
        return cached

    base = settings.github_api_base
    list_url = "{}/repos/{}/{}/commits".format(base, owner, repo)

    logger.info("GET  %s/%s commits?author=%s (limit=%d)", owner, repo, login, limit)

    async with _SEMAPHORE:
        client = _get_client()
        resp = await client.get(
            list_url,
            headers=_headers(),
            params={"author": login, "per_page": str(limit)},
        )
        if resp.status_code != 200:
            return []

        commits_list = resp.json()
        if not isinstance(commits_list, list) or not commits_list:
            return []

        results: List[Dict[str, Any]] = []

        first = commits_list[0]
        sha = str(first.get("sha", ""))
        fetched_detail = False

        if sha:
            detail_resp = await client.get(
                "{}/repos/{}/{}/commits/{}".format(base, owner, repo, sha),
                headers=_headers(),
            )
            if detail_resp.status_code == 200:
                detail = detail_resp.json()
                files = detail.get("files", [])
                files.sort(
                    key=lambda f: f.get("additions", 0) + f.get("deletions", 0),
                    reverse=True,
                )
                top_files = []
                for f in files[:2]:
                    patch = f.get("patch") or ""
                    if len(patch) > 800:
                        lines = patch.split("\n")
                        patch = "\n".join(lines[:25]) + "\n..."
                    top_files.append({
                        "filename": f.get("filename", ""),
                        "patch": patch,
                    })

                msg = detail.get("commit", {}).get("message", "")
                results.append({
                    "message": msg.split("\n")[0][:200],
                    "url": detail.get("html_url", ""),
                    "files": top_files,
                })
                fetched_detail = True

        if not fetched_detail:
            msg = first.get("commit", {}).get("message", "")
            results.append({
                "message": msg.split("\n")[0][:200],
                "url": first.get("html_url", ""),
                "files": [],
            })

        for c in commits_list[1:]:
            msg = c.get("commit", {}).get("message", "")
            results.append({
                "message": msg.split("\n")[0][:200],
                "url": c.get("html_url", ""),
                "files": [],
            })

        logger.info(
            "     %s/%s got %d commits for %s", owner, repo, len(results), login,
        )
        _cache_set(cache_key, results)
        return results


async def fetch_file_content(owner: str, repo: str, path: str) -> Optional[str]:
    """Fetch a single file from a repo via the Contents API."""
    logger.info("GET  %s/%s contents/%s", owner, repo, path)
    url = "{}/repos/{}/{}/contents/{}".format(
        settings.github_api_base, owner, repo, path
    )
    headers = {**_headers(), "Accept": "application/vnd.github.raw+json"}

    async with _SEMAPHORE:
        client = _get_client()
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.info(
                "     %s/%s %s not found (%d)", owner, repo, path, resp.status_code
            )
            return None

    logger.info("     %s/%s %s fetched (%d bytes)", owner, repo, path, len(resp.text))
    return resp.text
