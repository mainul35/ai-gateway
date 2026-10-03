"""A searchable index of code and documents, so the agent can answer from them instead of guessing.

Sources are listed in config/knowledge.yaml: folders on this machine, and every repository of some
GitHub owners, cloned shallow and refreshed on a schedule. Each text file is split into passages of a
few dozen lines, each passage is embedded by a small model in Ollama, and both the embedding and the
words go into Postgres (pgvector plus full-text search). A search runs both ways and merges them,
because a question in words finds passages by meaning while a function name or an error message
is found by its exact spelling, and either alone misses half of what is asked.

Indexing is incremental: a file whose content has not changed is not embedded again, and a file that
has gone has its passages deleted. It runs in the background, on demand from the admin API and on a
timer, one run at a time.

Nothing that looks like a credential is stored. Files whose names say they hold secrets are skipped,
and every passage is checked against the same patterns as the pre-push secret check
(scripts/check_secrets.py); one that matches is dropped.
"""
import asyncio
import base64
import fnmatch
import hashlib
import importlib.util
import logging
import os
import re
import time
from dataclasses import dataclass, field

import httpx
import yaml
from sqlalchemy import text

from app import github_access, settings
from app.db import engine
from utils.ollama_client import ollama_host

log = logging.getLogger("knowledge")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIMENSIONS = 1024           # qwen3-embedding:0.6b; changing the model means dropping the index
EMBED_BATCH = 16
MAX_FILE_BYTES = 256 * 1024
TARGET_CHARS = 1600         # a passage is cut near here, at a heading or a blank line if one is close
HARD_CHARS = 2400
OVERLAP_LINES = 4
QUERY_INSTRUCTION = ("Instruct: Given a question about software projects, their code, or the servers "
                     "they run on, retrieve the code or documentation that answers it\nQuery: ")

TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".rst", ".adoc",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".java", ".kt", ".kts",
    ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala", ".dart",
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".sql", ".graphql", ".proto",
    ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".bat", ".cmd",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".gradle", ".json", ".jinja", ".j2",
    ".tf", ".hcl", ".service", ".nginx",
}
TEXT_NAMES = {"dockerfile", "makefile", "readme", "license", "procfile", "caddyfile", "jenkinsfile",
              "docker-compose.yml", "compose.yml"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "dist", "build", "target", "out", ".next",
             ".nuxt", "__pycache__", ".idea", ".vscode", ".gradle", "coverage", "vendor", ".terraform",
             "site-packages", ".deploy-backup", ".deploy-state", "logs", ".mypy_cache", ".pytest_cache",
             ".angular", ".cache", "obj"}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock", "go.sum",
              "composer.lock", "gemfile.lock", "config.properties"}
# Files whose names say they hold secrets. Code that merely handles secrets (check_secrets.py) is
# still indexed; its passages go through the same credential patterns as everything else.
SECRET_FILES = (".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
                "id_rsa*", "id_ed25519*", "*credentials*", "*.kdbx", ".npmrc", ".pypirc", ".netrc",
                "*secret*.yml", "*secret*.yaml", "*secret*.json", "*secret*.properties", "*secret*.toml",
                "*secret*.ini", "*secret*.txt")


# --- configuration -----------------------------------------------------------------------------


def is_enabled():
    return settings.get_bool("knowledge.enabled", "KNOWLEDGE_ENABLED", True)


def embedding_model():
    return settings.get("knowledge.embedding.model", "KNOWLEDGE_EMBEDDING_MODEL") or "qwen3-embedding:0.6b"


def config_file():
    path = settings.get("knowledge.file", "KNOWLEDGE_FILE") or "config/knowledge.yaml"
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def load_config():
    try:
        with open(config_file(), encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("knowledge config unreadable: %s", e)
        return {}


def github_token(owner):
    """Set on the Tasks page (GitHub access), or by hand in config.properties."""
    return github_access.token(owner, "read") or settings.get("knowledge.github.token", "KNOWLEDGE_GITHUB_TOKEN") or ""


# --- schema ------------------------------------------------------------------------------------

SCHEMA = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """CREATE TABLE IF NOT EXISTS knowledge_sources (
         name TEXT PRIMARY KEY, kind TEXT NOT NULL, location TEXT NOT NULL, description TEXT,
         files INTEGER DEFAULT 0, chunks INTEGER DEFAULT 0, indexed_at TIMESTAMPTZ, error TEXT)""",
    """CREATE TABLE IF NOT EXISTS knowledge_files (
         source TEXT NOT NULL, path TEXT NOT NULL, sha TEXT NOT NULL, indexed_at TIMESTAMPTZ DEFAULT now(),
         PRIMARY KEY (source, path))""",
    f"""CREATE TABLE IF NOT EXISTS knowledge_chunks (
         id BIGSERIAL PRIMARY KEY, source TEXT NOT NULL, path TEXT NOT NULL,
         start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, content TEXT NOT NULL,
         embedding vector({DIMENSIONS}) NOT NULL,
         -- English stemming, so "deployed" finds deploy; the path's words too, since a file called
         -- bin/deploy.sh says what it is about better than most of its lines do
         tsv tsvector GENERATED ALWAYS AS
             (to_tsvector('english', replace(path, '/', ' ') || ' ' || content)) STORED)""",
    "CREATE INDEX IF NOT EXISTS knowledge_chunks_file ON knowledge_chunks (source, path)",
    "CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding ON knowledge_chunks "
    "USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS knowledge_chunks_words ON knowledge_chunks USING gin (tsv)",
]


async def ensure_schema():
    async with engine().begin() as connection:
        for statement in SCHEMA:
            await connection.execute(text(statement))


# --- secrets -----------------------------------------------------------------------------------

def _load_secret_rules():
    """The pre-push secret check's patterns, so the two never disagree about what a secret is."""
    path = os.path.join(PROJECT_ROOT, "scripts", "check_secrets.py")
    spec = importlib.util.spec_from_file_location("check_secrets", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.rules_for, module.PLACEHOLDER


_rules_for, _PLACEHOLDER = _load_secret_rules()


def looks_secret(path, content):
    for _label, pattern in _rules_for(path):
        for line in content.splitlines():
            match = pattern.search(line)
            if match and not _PLACEHOLDER.match(match.group("value").strip()):
                return True
    return False


def _secret_file(name):
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in SECRET_FILES)


# --- files and passages ------------------------------------------------------------------------


def wanted_files(root):
    """Text files under a folder worth indexing, as paths relative to it."""
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = sorted(d for d in subdirectories if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            lowered = name.lower()
            suffix = os.path.splitext(lowered)[1]
            if lowered in SKIP_NAMES or _secret_file(name) or lowered.endswith((".min.js", ".min.css")):
                continue
            if suffix not in TEXT_SUFFIXES and lowered not in TEXT_NAMES:
                continue
            full = os.path.join(directory, name)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES or os.path.islink(full):
                    continue
            except OSError:
                continue
            yield os.path.relpath(full, root).replace(os.sep, "/")


def read_text(full_path):
    try:
        with open(full_path, encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    if "\x00" in content:
        return None
    lines = content.splitlines() or [""]
    if sum(len(line) for line in lines) / len(lines) > 400:     # minified or generated
        return None
    return content


_BREAK = re.compile(r"^(#{1,6} |\s*$|(async )?def |class |function |export |public |private |func |fn |impl )")


def passages(content):
    """Splits a file into passages of a few dozen lines: (start_line, end_line, text), 1-based."""
    lines = content.splitlines()
    out, start, size = [], 0, 0
    for index, line in enumerate(lines):
        size += len(line) + 1
        at_break = index + 1 < len(lines) and _BREAK.match(lines[index + 1])
        if size >= HARD_CHARS or (size >= TARGET_CHARS and at_break):
            out.append((start + 1, index + 1, "\n".join(lines[start:index + 1])))
            start = max(index + 1 - OVERLAP_LINES, start + 1)
            size = sum(len(l) + 1 for l in lines[start:index + 1])
    if start < len(lines):
        tail = "\n".join(lines[start:])
        if tail.strip():
            out.append((start + 1, len(lines), tail))
    return [p for p in out if p[2].strip()]


# --- embeddings --------------------------------------------------------------------------------


async def embed(texts, query=False):
    """Vectors for some texts. On the GPU when there is room, on the CPU when a big model has it."""
    if query:
        texts = [QUERY_INSTRUCTION + t for t in texts]
    body = {"model": embedding_model(), "input": texts, "keep_alive": "5m",
            "options": {"num_ctx": 2048}}
    async with httpx.AsyncClient(timeout=600) as client:
        for attempt in ("gpu", "cpu"):
            if attempt == "cpu":
                body["options"]["num_gpu"] = 0
            response = await client.post(f"{ollama_host()}/api/embed", json=body)
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            if response.status_code < 400 and data.get("embeddings"):
                return data["embeddings"]
            problem = data.get("error") or response.text[:300]
            if attempt == "gpu" and "memory" in str(problem).lower():
                log.info("embedding on the GPU failed for lack of memory; using the CPU")
                continue
            raise RuntimeError(f"embedding failed: {problem}")
    raise RuntimeError("embedding failed")


def _vector(values):
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


# --- sources -----------------------------------------------------------------------------------


@dataclass
class Source:
    name: str
    kind: str                   # "folder" or "github-repo"
    root: str                   # directory on this machine
    description: str = ""
    clone: dict = field(default_factory=dict)   # for a GitHub repository: owner, repo, branch


async def _github_repos(client, owner, spec):
    """Every repository of a user or an organisation that the token can see."""
    token = github_token(owner)
    if not token:
        raise RuntimeError(f"no GitHub token for {owner}: add one on the Tasks page, under GitHub access")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    repos, page = [], 1
    while True:
        response = await client.get(f"https://api.github.com/orgs/{owner}/repos",
                                    params={"per_page": 100, "page": page, "type": "all"}, headers=headers)
        if response.status_code == 404:          # a user, not an organisation
            response = await client.get("https://api.github.com/user/repos",
                                        params={"per_page": 100, "page": page, "affiliation": "owner"},
                                        headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub said {response.status_code} listing {owner}'s repositories: "
                               f"{response.text[:200]}")
        batch = response.json()
        repos += [r for r in batch if r["owner"]["login"].lower() == owner.lower()]
        if len(batch) < 100:
            break
        page += 1
    excluded = {name.lower() for name in spec.get("exclude") or []}
    return [r for r in repos
            if r["name"].lower() not in excluded
            and (spec.get("include_forks") or not r.get("fork"))
            and (spec.get("include_archived") or not r.get("archived"))
            and r.get("size", 0) <= int(spec.get("max_repo_mb", 300)) * 1024]


async def resolve_sources():
    """The configured sources, with GitHub owners expanded into one source per repository.

    Returns (sources, problems): an owner without a token is a problem to report, not a reason to
    skip everything else.
    """
    config = load_config()
    checkout_dir = config.get("checkout_dir") or os.path.join(PROJECT_ROOT, "knowledge-repos")
    sources, problems = [], []
    async with httpx.AsyncClient(timeout=30) as client:
        for spec in config.get("sources") or []:
            kind = spec.get("kind")
            if kind == "folder":
                path = spec.get("path") or "."
                root = path if os.path.isabs(path) else os.path.normpath(os.path.join(PROJECT_ROOT, path))
                sources.append(Source(spec["name"], "folder", root, spec.get("description") or ""))
            elif kind == "github":
                for owner in spec.get("owners") or []:
                    try:
                        for repo in await _github_repos(client, owner, spec):
                            sources.append(Source(
                                f"{owner}/{repo['name']}", "github-repo",
                                os.path.join(checkout_dir, owner, repo["name"]), repo.get("description") or "",
                                {"owner": owner, "repo": repo["name"], "branch": repo.get("default_branch") or "main"}))
                    except (RuntimeError, httpx.HTTPError) as e:
                        problems.append(f"{owner}: {e}")
    return sources, problems


async def _git(args, cwd=None, token=""):
    """Runs git with the token as a header in the environment: never in a URL, a file, or argv."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"})
    process = await asyncio.create_subprocess_exec("git", *args, cwd=cwd, env=env,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(process.communicate(), timeout=600)
    if process.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {err.decode(errors='replace').strip()[-300:]}")


async def update_checkout(source):
    """Clones or fast-forwards a shallow copy of a repository's default branch."""
    clone = source.clone
    token = github_token(clone["owner"])
    url = f"https://github.com/{clone['owner']}/{clone['repo']}.git"
    if not os.path.isdir(os.path.join(source.root, ".git")):
        os.makedirs(os.path.dirname(source.root), exist_ok=True)
        await _git(["clone", "--quiet", "--depth", "1", "--branch", clone["branch"], url, source.root], token=token)
    else:
        await _git(["fetch", "--quiet", "--depth", "1", "origin", clone["branch"]], cwd=source.root, token=token)
        await _git(["reset", "--quiet", "--hard", "FETCH_HEAD"], cwd=source.root)


# --- indexing ----------------------------------------------------------------------------------

progress = {"running": False, "started_at": None, "finished_at": None, "source": None,
            "files_done": 0, "chunks_added": 0, "skipped_secret": 0, "problems": []}
_lock = asyncio.Lock()


async def _index_source(source):
    """Brings one source's passages in line with its files. Returns (files, chunks)."""
    async with engine().begin() as connection:
        known = dict((await connection.execute(
            text("SELECT path, sha FROM knowledge_files WHERE source = :s"), {"s": source.name})).all())

    present = set()
    for path in wanted_files(source.root):
        content = await asyncio.to_thread(read_text, os.path.join(source.root, path))
        if content is None:
            continue
        present.add(path)
        sha = hashlib.sha256(content.encode()).hexdigest()
        if known.get(path) == sha:
            continue
        kept = []
        for start, end, passage in passages(content):
            if looks_secret(path, passage):
                progress["skipped_secret"] += 1
                continue
            kept.append((start, end, passage))
        vectors = []
        for i in range(0, len(kept), EMBED_BATCH):
            batch = kept[i:i + EMBED_BATCH]
            vectors += await embed([f"{source.name}/{path}\n{p[2]}" for p in batch])
        async with engine().begin() as connection:
            await connection.execute(text("DELETE FROM knowledge_chunks WHERE source = :s AND path = :p"),
                                     {"s": source.name, "p": path})
            for (start, end, passage), vector in zip(kept, vectors):
                await connection.execute(text(
                    "INSERT INTO knowledge_chunks (source, path, start_line, end_line, content, embedding) "
                    "VALUES (:s, :p, :a, :b, :c, CAST(:v AS vector))"),
                    {"s": source.name, "p": path, "a": start, "b": end, "c": passage, "v": _vector(vector)})
            await connection.execute(text(
                "INSERT INTO knowledge_files (source, path, sha, indexed_at) VALUES (:s, :p, :h, now()) "
                "ON CONFLICT (source, path) DO UPDATE SET sha = :h, indexed_at = now()"),
                {"s": source.name, "p": path, "h": sha})
        progress["files_done"] += 1
        progress["chunks_added"] += len(kept)

    gone = set(known) - present
    async with engine().begin() as connection:
        for path in gone:
            await connection.execute(text("DELETE FROM knowledge_chunks WHERE source = :s AND path = :p"),
                                     {"s": source.name, "p": path})
            await connection.execute(text("DELETE FROM knowledge_files WHERE source = :s AND path = :p"),
                                     {"s": source.name, "p": path})
        counts = (await connection.execute(text(
            "SELECT count(DISTINCT path), count(*) FROM knowledge_chunks WHERE source = :s"),
            {"s": source.name})).one()
    return counts[0], counts[1]


async def _record(source, files=None, chunks=None, error=None):
    async with engine().begin() as connection:
        # asyncpg needs every parameter's type spelled out when it appears in more than one place
        await connection.execute(text(
            "INSERT INTO knowledge_sources (name, kind, location, description, files, chunks, indexed_at, error) "
            "VALUES (:n, :k, :l, :d, coalesce(CAST(:f AS integer), 0), coalesce(CAST(:c AS integer), 0), "
            "        CASE WHEN CAST(:e AS text) IS NULL THEN now() END, CAST(:e AS text)) "
            "ON CONFLICT (name) DO UPDATE SET kind = :k, location = :l, description = :d, "
            "files = coalesce(CAST(:f AS integer), knowledge_sources.files), "
            "chunks = coalesce(CAST(:c AS integer), knowledge_sources.chunks), "
            "indexed_at = CASE WHEN CAST(:e AS text) IS NULL THEN now() ELSE knowledge_sources.indexed_at END, "
            "error = CAST(:e AS text)"),
            {"n": source.name, "k": source.kind, "l": source.root, "d": source.description,
             "f": files, "c": chunks, "e": error})


async def reindex(only=None):
    """One indexing run over every source (or those whose name matches `only`). One at a time."""
    if _lock.locked():
        return False
    async with _lock:
        progress.update(running=True, started_at=time.time(), finished_at=None, source=None,
                        files_done=0, chunks_added=0, skipped_secret=0, problems=[])
        try:
            await ensure_schema()
            sources, problems = await resolve_sources()
            progress["problems"] += problems
            configured = {s.name for s in sources}
            for source in sources:
                if only and not fnmatch.fnmatch(source.name, only):
                    continue
                progress["source"] = source.name
                try:
                    if source.kind == "github-repo":
                        await update_checkout(source)
                    if not os.path.isdir(source.root):
                        raise RuntimeError(f"{source.root} is not a directory")
                    files, chunks = await _index_source(source)
                    await _record(source, files, chunks)
                    log.info("knowledge: %s indexed, %s files, %s passages", source.name, files, chunks)
                except Exception as e:
                    progress["problems"].append(f"{source.name}: {e}")
                    log.warning("knowledge: %s failed: %s", source.name, e)
                    await _record(source, error=str(e)[:500])
            # Sources no longer configured, or repositories that went away, leave the index
            if not only and not problems:
                async with engine().begin() as connection:
                    stale = [row[0] for row in (await connection.execute(
                        text("SELECT name FROM knowledge_sources"))).all() if row[0] not in configured]
                    for name in stale:
                        for table in ("knowledge_chunks", "knowledge_files"):
                            await connection.execute(text(f"DELETE FROM {table} WHERE source = :s"), {"s": name})
                        await connection.execute(text("DELETE FROM knowledge_sources WHERE name = :s"), {"s": name})
        finally:
            progress.update(running=False, finished_at=time.time(), source=None)
    return True


def start_reindex(only=None):
    """Starts a run in the background. False when one is already going."""
    if _lock.locked():
        return False
    asyncio.create_task(reindex(only))
    return True


async def refresh_loop():
    """Re-indexes every knowledge.refresh.hours, so pushed changes reach the index on their own."""
    await asyncio.sleep(600)             # let a fresh deploy settle; a restart is not a reason to index
    while True:
        hours = float(settings.get("knowledge.refresh.hours", "KNOWLEDGE_REFRESH_HOURS") or 0)
        if is_enabled() and hours > 0:
            try:
                last = await last_indexed()
                if last is None or time.time() - last > hours * 3600:
                    await reindex()
            except Exception as e:
                log.warning("knowledge refresh failed: %s", e)
        await asyncio.sleep(1800)


async def last_indexed():
    async with engine().begin() as connection:
        value = (await connection.execute(text(
            "SELECT extract(epoch FROM min(indexed_at)) FROM knowledge_sources WHERE error IS NULL"))).scalar()
    return float(value) if value is not None else None


# --- reading -----------------------------------------------------------------------------------


async def list_sources():
    async with engine().begin() as connection:
        rows = (await connection.execute(text(
            "SELECT name, kind, description, files, chunks, indexed_at, error FROM knowledge_sources "
            "ORDER BY name"))).mappings().all()
    return [{**row, "indexed_at": row["indexed_at"].isoformat() if row["indexed_at"] else None} for row in rows]


# Words as the query gives them. One spelled with underscores - an identifier like ensure_running -
# becomes a phrase of its parts, since to_tsvector splits it there and only adjacency keeps the match
# to that identifier rather than to every passage that says "ensure" and "running" somewhere.
_WORD = re.compile(r"[A-Za-z0-9_]{2,}")


def _tsquery(query):
    terms = []
    for word in dict.fromkeys(w.lower() for w in _WORD.findall(query)):
        parts = [p for p in word.split("_") if p]
        if not parts or (len(parts) == 1 and parts[0] in _STOP):
            continue
        terms.append(parts[0] if len(parts) == 1 else "(" + " <-> ".join(parts) + ")")
    return " | ".join(terms[:16])
_STOP = {"the", "and", "for", "are", "was", "how", "what", "why", "does", "this", "that", "with", "from",
         "where", "which", "when", "who", "can", "into", "about", "there", "have", "has", "use", "used"}


async def search(query, source=None, limit=8):
    """Passages that answer a question, best first: meaning and exact words, merged by rank."""
    vector = _vector((await embed([query], query=True))[0])
    tsquery = _tsquery(query)
    pattern = source.replace("*", "%") if source else None
    sql = text("""
        WITH by_meaning AS (
            SELECT id, row_number() OVER (ORDER BY embedding <=> CAST(:v AS vector)) AS r
            FROM knowledge_chunks WHERE (CAST(:src AS text) IS NULL OR source ILIKE :src)
            ORDER BY embedding <=> CAST(:v AS vector) LIMIT 40),
        by_words AS (
            SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS r
            FROM knowledge_chunks, to_tsquery('english', CAST(:tq AS text)) q
            WHERE CAST(:tq AS text) <> '' AND tsv @@ q AND (CAST(:src AS text) IS NULL OR source ILIKE :src)
            ORDER BY ts_rank_cd(tsv, q) DESC LIMIT 40)
        SELECT c.source, c.path, c.start_line, c.end_line, c.content,
               coalesce(1.0 / (60 + m.r), 0) + coalesce(1.0 / (60 + w.r), 0) AS score
        FROM knowledge_chunks c
        LEFT JOIN by_meaning m ON m.id = c.id LEFT JOIN by_words w ON w.id = c.id
        WHERE m.id IS NOT NULL OR w.id IS NOT NULL
        ORDER BY score DESC LIMIT :k""")
    async with engine().begin() as connection:
        rows = (await connection.execute(sql, {"v": vector, "src": pattern, "tq": tsquery or "",
                                               "k": max(1, min(int(limit), 20))})).mappings().all()
    return [dict(row, score=round(float(row["score"]), 4)) for row in rows]


async def read_file(source, path, start_line=1, end_line=None):
    """Lines of an indexed file, for when a passage is not enough. Only files that are indexed."""
    async with engine().begin() as connection:
        indexed = (await connection.execute(text(
            "SELECT 1 FROM knowledge_files WHERE source = :s AND path = :p"), {"s": source, "p": path})).scalar()
        location = (await connection.execute(text(
            "SELECT location FROM knowledge_sources WHERE name = :s"), {"s": source})).scalar()
    if not indexed or not location:
        raise ValueError(f"{source}/{path} is not in the index; search for it first")
    root = os.path.realpath(location)
    full = os.path.realpath(os.path.join(root, path))
    if not full.startswith(root + os.sep):
        raise ValueError("that path is outside the source")
    content = await asyncio.to_thread(read_text, full)
    if content is None:
        raise ValueError(f"{source}/{path} could not be read")
    lines = content.splitlines()
    start = max(1, int(start_line or 1))
    end = min(len(lines), int(end_line or start + 199), start + 399)
    excerpt = "\n".join(lines[start - 1:end])
    if looks_secret(path, excerpt):
        raise ValueError("those lines look like they contain a credential, so they are not shown")
    return {"source": source, "path": path, "start_line": start, "end_line": end,
            "total_lines": len(lines), "content": excerpt}
