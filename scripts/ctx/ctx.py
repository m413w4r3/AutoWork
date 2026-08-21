#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy>=1.26", "openai>=1.60"]
# ///
"""Index hybride local pour agents de code.

Objectif : localiser rapidement les petites plages de code à lire avant toute
exploration large du dépôt.

Principes :
- chunks symboliques, sans troncature silencieuse ;
- couverture complète des fichiers indexés ;
- cache d'embeddings isolé par modèle + version d'index ;
- recherche hybride dense + lexicale (symboles/chemins/identifiants) ;
- sortie compacte ``path:start-end  symbol`` ;
- index incrémental et écritures atomiques ;
- verrou de build pour éviter les hooks concurrents.

Exemples :

    uv run scripts/ctx/ctx.py build
    uv run scripts/ctx/ctx.py query "cycle de vie conversation release" -k 8
    uv run scripts/ctx/ctx.py query "external_locator cleanup" --path chatgpt-bridge/
    uv run scripts/ctx/ctx.py query "ConversationPolicy" --lexical-only
    uv run scripts/ctx/ctx.py status
    uv run scripts/ctx/ctx.py doctor
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

import numpy as np
from openai import APIConnectionError, APIStatusError, OpenAI

try:  # POSIX ; AutoWork est utilisé sous Linux.
    import fcntl
except ImportError:  # pragma: no cover - fallback Windows
    fcntl = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- config

INDEX_SCHEMA = 2
EMBED_SCHEMA = 2

DEFAULT_MODEL = "Qwen3-Embedding-8B-ChapsVision"
QUERY_INSTRUCTION = (
    "Given a natural-language description of a code change, retrieve the "
    "source code chunks that must be read or modified to implement it."
)

MAX_FILE_BYTES = 1_500_000
MAX_CHUNK_LINES = 110
WINDOW_LINES = 64
WINDOW_OVERLAP = 10
MAX_CHUNK_CHARS = 5_500
BATCH = 32

DEFAULT_K = 8
DEFAULT_PER_FILE = 2

# Le score lexical est volontairement important pour le code : les identifiants
# exacts (ConversationPolicy, external_locator...) sont extrêmement informatifs.
DENSE_WEIGHT = 0.68
LEXICAL_WEIGHT = 0.27
META_WEIGHT = 0.05

INCLUDE_SUFFIXES = {
    ".py", ".pyi",
    ".ts", ".tsx", ".js", ".jsx",
    ".md", ".txt",
    ".sql", ".toml", ".yaml", ".yml", ".json",
    ".css", ".scss", ".html", ".sh",
}
INCLUDE_NAMES = {
    "Makefile", "Dockerfile", "compose.yaml", "compose.yml",
}

EXCLUDE_DIR_PARTS = {
    ".git", ".venv", "venv", "node_modules", "var", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__",
    ".roo", ".claude", ".ai", ".agents", ".codex", "chatGPT_Answers", ".secrets",
    ".next", "coverage", "htmlcov",
}

# Fort bruit, faible valeur de navigation. Les migrations restent indexées :
# sur AutoWork elles sont parfois nécessaires pour les tâches de schéma.
EXCLUDE_PREFIXES = {
    "backend/tests/fixtures/",
    "frontend/node_modules/",
    "refacto_baseLine/",
}

EXCLUDE_BASENAMES = {
    ".llms_key", ".env", "auth.json",
    "uv.lock", "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "poetry.lock",
}

# Certains fichiers historiques sont utiles, mais doivent perdre face au code actif.
PATH_PRIORS: tuple[tuple[str, float], ...] = (
    ("backend/src/", 1.00),
    ("frontend/src/", 1.00),
    ("chatgpt-bridge/", 1.00),
    ("backend/tests/", 0.94),
    ("frontend/e2e/", 0.92),
    ("docs/adr/", 0.93),
    ("docs/", 0.88),
    ("backend/migrations/versions/", 0.78),
)

HEADING = re.compile(r"^#{1,4}\s+(.+?)\s*$")
TS_DECL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?"
    r"(?:function|class|interface|type|enum|const|let|var)\s+"
    r"([A-Za-z_$][\w$]*)"
)
TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+")
CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


# --------------------------------------------------------------------------- paths / io


def find_root() -> Path:
    """Trouve la racine Git, avec fallback pour scripts/ctx/ctx.py."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
            check=True,
            timeout=2,
        )
        root = Path(proc.stdout.strip()).resolve()
        if root.exists():
            return root
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(__file__).resolve().parents[2]


ROOT = find_root()
STORE = ROOT / "var" / "ctx"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def atomic_save_npy(path: Path, matrix: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".npy", dir=path.parent)
    os.close(fd)
    try:
        np.save(tmp_name, matrix)
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


@contextlib.contextmanager
def build_lock() -> Iterator[None]:
    STORE.mkdir(parents=True, exist_ok=True)
    lock_path = STORE / ".build.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# --------------------------------------------------------------------------- credentials


def make_client() -> OpenAI:
    base_url = os.getenv("BASE_URL", "").strip()
    api_key = os.getenv("EMBEDDING_API_KEY", "").strip()
    if not base_url or not api_key:
        raise RuntimeError(
            "BASE_URL and EMBEDDING_API_KEY environment variables are required. "
            "Please set them before running this command."
        )
    return OpenAI(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        timeout=180.0,
        max_retries=2,
    )


# --------------------------------------------------------------------------- model


@dataclass(frozen=True)
class Chunk:
    path: str
    start: int
    end: int
    symbol: str
    kind: str
    key: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    symbol: str
    kind: str


@dataclass(frozen=True)
class Ranked:
    chunk: Chunk
    dense: float
    lexical: float
    meta: float
    score: float


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_id(model: str) -> str:
    return digest(f"embed-schema={EMBED_SCHEMA}\nmodel={model}")[:20]


def cache_dir(model: str) -> Path:
    return STORE / "cache" / cache_id(model)


# --------------------------------------------------------------------------- tokenisation


def split_identifier(text: str) -> list[str]:
    out: list[str] = []
    # Garde aussi les formes complètes : external_locator et external locator
    # doivent pouvoir se retrouver mutuellement.
    for raw in re.split(r"[^\wÀ-ÖØ-öø-ÿ$]+", text, flags=re.UNICODE):
        if not raw:
            continue
        raw = raw.strip("_$")
        if not raw:
            continue
        out.append(raw.casefold())
        for snake in raw.replace("-", "_").split("_"):
            if not snake:
                continue
            for part in CAMEL_BOUNDARY.split(snake):
                if part:
                    out.append(part.casefold())
    return out


def lexical_terms(path: str, symbol: str, body: str) -> tuple[str, ...]:
    terms: set[str] = set()
    # path/symbol sont répétés conceptuellement, mais on ne stocke qu'un set :
    # leur importance est ajoutée séparément via meta_score().
    for source in (path, symbol, body):
        terms.update(split_identifier(source))
        # Ajoute aussi les tokens textuels accentués usuels.
        terms.update(m.group(0).casefold() for m in TOKEN_RE.finditer(source))
    return tuple(sorted(t for t in terms if len(t) >= 2 or t.isdigit()))


def query_terms(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(split_identifier(text)))


# --------------------------------------------------------------------------- file enumeration


def git_files() -> list[Path]:
    """Fichiers versionnés + non suivis non ignorés, sans rglob massif."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=10,
        )
        names = [p.decode("utf-8", errors="surrogateescape") for p in proc.stdout.split(b"\0") if p]
        return [ROOT / name for name in names]
    except (OSError, subprocess.SubprocessError):
        return [p for p in ROOT.rglob("*") if p.is_file()]


def is_included(path: Path) -> bool:
    try:
        rel = path.relative_to(ROOT).as_posix()
    except ValueError:
        return False

    parts = path.relative_to(ROOT).parts
    if any(part in EXCLUDE_DIR_PARTS for part in parts[:-1]):
        return False
    if any(rel.startswith(prefix) for prefix in EXCLUDE_PREFIXES):
        return False
    if path.name in EXCLUDE_BASENAMES:
        return False
    if any(part in {".secrets", ".llms_key", ".env"} for part in parts):
        return False
    if path.suffix not in INCLUDE_SUFFIXES and path.name not in INCLUDE_NAMES:
        return False
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return False
    except OSError:
        return False
    return True


def iter_files() -> list[Path]:
    return sorted({p.resolve() for p in git_files() if p.is_file() and is_included(p)})


def file_signature(path: Path) -> str:
    """Signature rapide pour détecter un index potentiellement obsolète."""
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    rel = path.relative_to(ROOT).as_posix()
    return f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}"


def repo_signature(paths: list[Path]) -> str:
    return digest("\n".join(file_signature(p) for p in paths))


# --------------------------------------------------------------------------- span helpers


def windows(start: int, end: int, *, size: int = WINDOW_LINES, overlap: int = WINDOW_OVERLAP) -> list[tuple[int, int]]:
    if end < start:
        return []
    out: list[tuple[int, int]] = []
    i = start
    step = max(size - overlap, 1)
    while i <= end:
        j = min(i + size - 1, end)
        out.append((i, j))
        if j == end:
            break
        i += step
    return out


def split_span_to_limits(rel: str, lines: list[str], span: Span) -> list[Span]:
    """Découpe un span sans jamais mentir sur la plage réellement encodée."""
    if span.end < span.start:
        return []

    header = f"# {rel} :: {span.symbol} [{span.kind}]\n"
    max_body_chars = max(MAX_CHUNK_CHARS - len(header), 256)

    out: list[Span] = []
    start = span.start
    hard_end = span.end

    while start <= hard_end:
        end = min(start + MAX_CHUNK_LINES - 1, hard_end)

        # Réduit jusqu'à respecter la limite caractères.
        while end > start:
            body_chars = sum(len(lines[i - 1]) + 1 for i in range(start, end + 1))
            if body_chars <= max_body_chars:
                break
            end -= 1

        # Une ligne gigantesque reste une ligne entière : pas de troncature silencieuse.
        out.append(Span(start, end, span.symbol, span.kind))
        if end >= hard_end:
            break

        # Petit overlap pour les spans forcés, mais la plage imprimée reste exacte.
        next_start = max(end - WINDOW_OVERLAP + 2, start + 1)
        start = next_start

    return out


def contiguous_uncovered(total_lines: int, covered: set[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    start: int | None = None
    for line in range(1, total_lines + 1):
        if line not in covered and start is None:
            start = line
        if line in covered and start is not None:
            out.append((start, line - 1))
            start = None
    if start is not None:
        out.append((start, total_lines))
    return out


# --------------------------------------------------------------------------- chunkers


def split_python(source: str) -> list[Span]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines = source.splitlines()
    if not lines:
        return []

    spans: list[Span] = []
    covered: set[int] = set()
    class_ranges: list[tuple[int, int, str]] = []

    def node_range(node: ast.AST) -> tuple[int, int] | None:
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if start is None or end is None:
            return None
        decorators = getattr(node, "decorator_list", [])
        if decorators:
            start = min(start, *(d.lineno for d in decorators))
        return start, end

    def emit_node(node: ast.AST, symbol: str, kind: str) -> None:
        rng = node_range(node)
        if rng is None:
            return
        start, end = rng
        covered.update(range(start, end + 1))
        spans.append(Span(start, end, symbol, kind))

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            rng = node_range(node)
            if rng is None:
                continue
            class_start, class_end = rng
            class_ranges.append((class_start, class_end, node.name))

            methods = [
                sub for sub in node.body
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            if class_end - class_start + 1 <= MAX_CHUNK_LINES:
                emit_node(node, node.name, "class")
                continue

            # Grande classe : en-tête séparé puis méthodes. Les trous entre
            # méthodes (attributs, classes imbriquées, constantes...) seront
            # récupérés ensuite comme Class.<body>.
            if methods:
                first_method = node_range(methods[0])
                header_end = (first_method[0] - 1) if first_method else class_start
                if header_end >= class_start:
                    covered.update(range(class_start, header_end + 1))
                    spans.append(Span(class_start, header_end, node.name, "class-header"))
            else:
                covered.update(range(class_start, class_end + 1))
                spans.append(Span(class_start, class_end, node.name, "class-body"))

            for method in methods:
                emit_node(method, f"{node.name}.{method.name}", "method")

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            emit_node(node, node.name, "function")

    # Garantit 100 % de couverture, y compris constantes entre deux fonctions,
    # attributs d'une grande classe, imports tardifs, etc.
    for start, end in contiguous_uncovered(len(lines), covered):
        owner = next(
            (name for c_start, c_end, name in class_ranges if c_start <= start <= c_end),
            None,
        )
        if owner is not None:
            spans.append(Span(start, end, f"{owner}.<body>", "class-body"))
        else:
            spans.append(Span(start, end, "<module>", "module"))

    return sorted(spans, key=lambda s: (s.start, s.end, s.symbol))


def split_markdown(source: str) -> list[Span]:
    lines = source.splitlines()
    if not lines:
        return []
    marks = [
        (i + 1, match.group(1).strip())
        for i, line in enumerate(lines)
        if (match := HEADING.match(line))
    ]
    if not marks:
        return [Span(1, len(lines), "<doc>", "doc")]

    out: list[Span] = []
    if marks[0][0] > 1:
        out.append(Span(1, marks[0][0] - 1, "<preamble>", "doc"))

    for idx, (start, title) in enumerate(marks):
        end = marks[idx + 1][0] - 1 if idx + 1 < len(marks) else len(lines)
        out.append(Span(start, max(start, end), title, "doc"))
    return out


def split_ts_like(source: str) -> list[Span]:
    """Découpage top-level simple mais robuste aux composants const TSX.

    On ne prétend pas parser JavaScript : on utilise les déclarations sans
    indentation comme frontières. Le point important est de garantir la
    couverture, puis de borner chaque span exactement.
    """
    lines = source.splitlines()
    if not lines:
        return []

    marks: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines, start=1):
        # Les déclarations top-level usuelles sont non indentées. Cela évite de
        # découper chaque const local à l'intérieur d'un composant React.
        if line[:1].isspace():
            continue
        if match := TS_DECL.match(line):
            keyword_match = re.search(r"\b(function|class|interface|type|enum|const|let|var)\b", line)
            kind = keyword_match.group(1) if keyword_match else "code"
            marks.append((i, match.group(1), kind))

    if not marks:
        return [Span(1, len(lines), "<module>", "code")]

    out: list[Span] = []
    if marks[0][0] > 1:
        out.append(Span(1, marks[0][0] - 1, "<module>", "header"))

    for idx, (start, name, kind) in enumerate(marks):
        end = marks[idx + 1][0] - 1 if idx + 1 < len(marks) else len(lines)
        out.append(Span(start, max(start, end), name, kind))
    return out


def raw_spans(path: Path, source: str) -> list[Span]:
    suffix = path.suffix.lower()
    lines = source.splitlines()
    if suffix in {".py", ".pyi"}:
        return split_python(source) or [Span(1, len(lines), "<module>", "code")]
    if suffix in {".md", ".txt"}:
        return split_markdown(source)
    if suffix in {".ts", ".tsx", ".js", ".jsx"}:
        return split_ts_like(source)
    return [Span(1, len(lines), "<file>", "config")]


def chunk_text(rel: str, span: Span, lines: list[str]) -> str:
    body = "\n".join(lines[span.start - 1: span.end])
    text = f"# {rel} :: {span.symbol} [{span.kind}]\n{body}"
    # split_span_to_limits doit avoir garanti cette propriété. Une assertion
    # vaut mieux qu'une troncature silencieuse.
    if len(text) > MAX_CHUNK_CHARS and span.start != span.end:
        raise AssertionError(f"chunk trop long après split: {rel}:{span.start}-{span.end}")
    return text


def build_chunks() -> tuple[list[Chunk], dict[str, str], str, list[Path]]:
    chunks: list[Chunk] = []
    texts: dict[str, str] = {}
    paths = iter_files()

    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not source.strip():
            continue

        lines = source.splitlines()
        if not lines:
            continue

        for raw in raw_spans(path, source):
            start = max(1, min(raw.start, len(lines)))
            end = max(start, min(raw.end, len(lines)))
            normalized = Span(start, end, raw.symbol, raw.kind)

            for span in split_span_to_limits(rel, lines, normalized):
                text = chunk_text(rel, span, lines)
                if len(text.strip()) < 32:
                    continue
                key = digest(text)
                terms = lexical_terms(rel, span.symbol, text)
                texts[key] = text
                chunks.append(
                    Chunk(
                        path=rel,
                        start=span.start,
                        end=span.end,
                        symbol=span.symbol,
                        kind=span.kind,
                        key=key,
                        terms=terms,
                    )
                )

    # Déduplication exacte de métadonnées, tout en conservant plusieurs chunks
    # pouvant partager le même texte/embedding.
    unique: dict[tuple[str, int, int, str, str], Chunk] = {}
    for chunk in chunks:
        ident = (chunk.path, chunk.start, chunk.end, chunk.symbol, chunk.kind)
        unique[ident] = chunk

    final = sorted(unique.values(), key=lambda c: (c.path, c.start, c.end, c.symbol))
    return final, texts, repo_signature(paths), paths


# --------------------------------------------------------------------------- embeddings / cache


def normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return matrix / np.clip(norms, 1e-12, None)


def embed(client: OpenAI, model: str, texts: list[str], *, quiet: bool = False) -> np.ndarray:
    out: list[list[float]] = []
    for start in range(0, len(texts), BATCH):
        batch = texts[start:start + BATCH]
        attempt = 0
        while True:
            try:
                response = client.embeddings.create(model=model, input=batch)
                break
            except (APIStatusError, APIConnectionError) as exc:
                attempt += 1
                if attempt >= 3:
                    raise RuntimeError(f"échec embedding après 3 tentatives: {exc}") from exc
                time.sleep(2 * attempt)

        ordered = sorted(response.data, key=lambda item: item.index)
        out.extend(item.embedding for item in ordered)
        if not quiet:
            done = min(start + BATCH, len(texts))
            print(f"\r  embedding {done}/{len(texts)}", end="", file=sys.stderr)

    if texts and not quiet:
        print(file=sys.stderr)
    if not out:
        return np.empty((0, 0), dtype=np.float32)
    return normalize(np.asarray(out, dtype=np.float32))


def load_cache(model: str) -> dict[str, np.ndarray]:
    directory = cache_dir(model)
    meta_path = directory / "meta.json"
    vec_path = directory / "vectors.npy"
    key_path = directory / "keys.json"
    if not meta_path.exists() or not vec_path.exists() or not key_path.exists():
        return {}

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("model") != model or meta.get("embed_schema") != EMBED_SCHEMA:
            return {}
        vectors = np.load(vec_path)
        keys = json.loads(key_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}

    if vectors.ndim != 2 or len(keys) != vectors.shape[0]:
        return {}
    return {str(key): vectors[i] for i, key in enumerate(keys)}


def save_cache(model: str, cache: dict[str, np.ndarray]) -> None:
    if not cache:
        return
    directory = cache_dir(model)
    directory.mkdir(parents=True, exist_ok=True)
    keys = sorted(cache)
    matrix = np.stack([cache[key] for key in keys]).astype(np.float32)
    atomic_save_npy(directory / "vectors.npy", matrix)
    atomic_write_text(directory / "keys.json", json.dumps(keys, ensure_ascii=False))
    atomic_write_text(
        directory / "meta.json",
        json.dumps(
            {
                "model": model,
                "embed_schema": EMBED_SCHEMA,
                "dim": int(matrix.shape[1]),
                "vectors": int(matrix.shape[0]),
            },
            indent=2,
            ensure_ascii=False,
        ),
    )


# --------------------------------------------------------------------------- build / index io


def manifest_path() -> Path:
    return STORE / "manifest.json"


def chunks_path() -> Path:
    return STORE / "chunks.jsonl"


def write_index(chunks: list[Chunk], model: str, signature: str, cache: dict[str, np.ndarray]) -> None:
    STORE.mkdir(parents=True, exist_ok=True)

    chunk_lines = "".join(
        json.dumps(
            {
                **asdict(chunk),
                "terms": list(chunk.terms),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
        for chunk in chunks
    )
    atomic_write_text(chunks_path(), chunk_lines)

    used_vectors = [cache[c.key] for c in chunks if c.key in cache]
    dim = int(used_vectors[0].shape[0]) if used_vectors else 0
    atomic_write_text(
        manifest_path(),
        json.dumps(
            {
                "index_schema": INDEX_SCHEMA,
                "embed_schema": EMBED_SCHEMA,
                "model": model,
                "dim": dim,
                "chunks": len(chunks),
                "unique_texts": len({c.key for c in chunks}),
                "cached_vectors": len(cache),
                "files": len({c.path for c in chunks}),
                "repo_signature": signature,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            indent=2,
            ensure_ascii=False,
        ),
    )


def do_build(model: str, *, full: bool = False, quiet: bool = False) -> dict[str, object]:
    with build_lock():
        chunks, texts, signature, _ = build_chunks()
        cache = {} if full else load_cache(model)
        missing = [key for key in texts if key not in cache]

        if not quiet:
            print(
                f"{len(chunks)} chunks / {len(texts)} textes uniques — "
                f"{len(missing)} à encoder, {len(texts) - len(missing)} en cache.",
                file=sys.stderr,
            )

        if missing:
            client = make_client()
            vectors = embed(client, model, [texts[key] for key in missing], quiet=quiet)
            for key, vector in zip(missing, vectors, strict=True):
                cache[key] = vector

        # Prune orphaned vectors: keep only embeddings for current texts
        wanted = set(texts)
        cache = {key: vector for key, vector in cache.items() if key in wanted}

        save_cache(model, cache)
        write_index(chunks, model, signature, cache)

        dim = int(next(iter(cache.values())).shape[0]) if cache else 0
        if not quiet:
            print(
                f"OK — {len(chunks)} chunks / {len({c.path for c in chunks})} fichiers, dim={dim}.",
                file=sys.stderr,
            )
        return {
            "chunks": len(chunks),
            "files": len({c.path for c in chunks}),
            "missing": len(missing),
            "dim": dim,
            "signature": signature,
        }


def load_manifest() -> dict[str, object]:
    path = manifest_path()
    if not path.exists():
        raise RuntimeError("index absent. Lance `ctx.py build`.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("manifest illisible ; relance `ctx.py build --full`.") from exc
    if data.get("index_schema") != INDEX_SCHEMA:
        raise RuntimeError("version d'index incompatible ; relance `ctx.py build --full`.")
    return data


def load_chunks() -> list[Chunk]:
    path = chunks_path()
    if not path.exists():
        raise RuntimeError("chunks absents ; relance `ctx.py build`.")
    out: list[Chunk] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = json.loads(line)
            raw["terms"] = tuple(raw.get("terms", []))
            out.append(Chunk(**raw))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("chunks illisibles ; relance `ctx.py build --full`.") from exc
    return out


def index_stale(manifest: dict[str, object] | None = None) -> tuple[bool, str]:
    if manifest is None:
        try:
            manifest = load_manifest()
        except RuntimeError as exc:
            return True, str(exc)
    paths = iter_files()
    current = repo_signature(paths)
    expected = str(manifest.get("repo_signature", ""))
    if current != expected:
        return True, "fichiers indexables modifiés depuis le dernier build"
    return False, "index à jour"


def load_index(model: str) -> tuple[list[Chunk], np.ndarray, dict[str, object]]:
    manifest = load_manifest()
    built_model = str(manifest.get("model", ""))
    if built_model != model:
        raise RuntimeError(
            f"index construit avec {built_model!r}, requête demandée avec {model!r}. "
            "Relance `ctx.py build --full --model ...`."
        )

    chunks = load_chunks()
    cache = load_cache(model)
    rows: list[np.ndarray] = []
    kept: list[Chunk] = []
    for chunk in chunks:
        vector = cache.get(chunk.key)
        if vector is None:
            continue
        kept.append(chunk)
        rows.append(vector)

    if not rows:
        raise RuntimeError("index vide ou cache désynchronisé ; relance `ctx.py build --full`.")
    matrix = np.stack(rows).astype(np.float32)
    if matrix.ndim != 2:
        raise RuntimeError("matrice d'embeddings invalide.")

    manifest_dim = int(manifest.get("dim", 0) or 0)
    if manifest_dim and matrix.shape[1] != manifest_dim:
        raise RuntimeError("dimension d'embedding incohérente ; relance `ctx.py build --full`.")
    return kept, matrix, manifest


# --------------------------------------------------------------------------- ranking


def path_prior(path: str) -> float:
    for prefix, prior in PATH_PRIORS:
        if path.startswith(prefix):
            return prior
    return 0.90


def lexical_scores(chunks: list[Chunk], text: str) -> np.ndarray:
    q_terms = query_terms(text)
    if not q_terms:
        return np.zeros(len(chunks), dtype=np.float32)

    qset = set(q_terms)
    doc_sets = [set(chunk.terms) for chunk in chunks]
    df = Counter(term for terms in doc_sets for term in (qset & terms))
    n = max(len(chunks), 1)
    idf = {
        term: math.log(1.0 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
        for term in qset
    }
    denom = sum(idf.values()) or 1.0

    scores = np.zeros(len(chunks), dtype=np.float32)
    for i, terms in enumerate(doc_sets):
        matched = qset & terms
        scores[i] = float(sum(idf[t] for t in matched) / denom)
    return scores


def meta_scores(chunks: list[Chunk], text: str) -> np.ndarray:
    q = text.casefold().strip()
    q_tokens = set(query_terms(text))
    out = np.zeros(len(chunks), dtype=np.float32)

    for i, chunk in enumerate(chunks):
        symbol = chunk.symbol.casefold()
        path = chunk.path.casefold()
        symbol_tokens = set(split_identifier(chunk.symbol))
        path_tokens = set(split_identifier(chunk.path))

        score = 0.0
        if q and (q in symbol or symbol in q):
            score += 0.55
        if q and q in path:
            score += 0.35
        if q_tokens and symbol_tokens:
            score += 0.45 * len(q_tokens & symbol_tokens) / len(q_tokens)
        if q_tokens and path_tokens:
            score += 0.25 * len(q_tokens & path_tokens) / len(q_tokens)
        score *= path_prior(chunk.path)
        out[i] = min(score, 1.0)
    return out


def overlap_ratio(a: Chunk, b: Chunk) -> float:
    if a.path != b.path:
        return 0.0
    left = max(a.start, b.start)
    right = min(a.end, b.end)
    if right < left:
        return 0.0
    overlap = right - left + 1
    shorter = min(a.end - a.start + 1, b.end - b.start + 1)
    return overlap / max(shorter, 1)


def select_results(
    ranked: list[Ranked],
    *,
    k: int,
    per_file: int,
    path_prefixes: list[str] | None,
    relative_floor: float,
) -> list[Ranked]:
    picked: list[Ranked] = []
    seen_per_file: Counter[str] = Counter()
    best: float | None = None

    for item in ranked:
        chunk = item.chunk
        if path_prefixes and not any(chunk.path.startswith(prefix) for prefix in path_prefixes):
            continue
        if seen_per_file[chunk.path] >= per_file:
            continue
        if any(overlap_ratio(chunk, previous.chunk) >= 0.72 for previous in picked):
            continue

        if best is None:
            best = item.score
        elif len(picked) >= 3 and relative_floor > 0 and item.score < best * relative_floor:
            # Un seuil relatif est plus portable entre modèles qu'un cosine absolu.
            break

        picked.append(item)
        seen_per_file[chunk.path] += 1
        if len(picked) >= k:
            break
    return picked


def rank_chunks(
    chunks: list[Chunk],
    matrix: np.ndarray,
    text: str,
    *,
    model: str,
    lexical_only: bool,
    no_instruct: bool,
) -> list[Ranked]:
    lexical = lexical_scores(chunks, text)
    meta = meta_scores(chunks, text)

    if lexical_only:
        dense = np.zeros(len(chunks), dtype=np.float32)
        scores = 0.90 * lexical + 0.10 * meta
    else:
        query_text = text
        if not no_instruct:
            query_text = f"Instruct: {QUERY_INSTRUCTION}\nQuery: {text}"
        client = make_client()
        query_vector = embed(client, model, [query_text], quiet=True)[0]
        if query_vector.shape[0] != matrix.shape[1]:
            raise RuntimeError(
                f"dimension requête={query_vector.shape[0]} != index={matrix.shape[1]} ; "
                "vérifie le modèle puis rebuild --full."
            )
        dense = matrix @ query_vector
        # cosine [-1,1] -> [0,1] pour rendre le mélange lisible.
        dense01 = np.clip((dense + 1.0) / 2.0, 0.0, 1.0)
        scores = DENSE_WEIGHT * dense01 + LEXICAL_WEIGHT * lexical + META_WEIGHT * meta

    # Prior de chemin léger, appliqué après fusion.
    scores = scores * np.asarray([path_prior(c.path) for c in chunks], dtype=np.float32)
    order = np.argsort(-scores)
    return [
        Ranked(
            chunk=chunks[int(i)],
            dense=float(dense[int(i)]),
            lexical=float(lexical[int(i)]),
            meta=float(meta[int(i)]),
            score=float(scores[int(i)]),
        )
        for i in order
    ]


# --------------------------------------------------------------------------- commands


def cmd_build(args: argparse.Namespace) -> int:
    try:
        do_build(args.model, full=args.full, quiet=args.quiet)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


def maybe_refresh(args: argparse.Namespace) -> None:
    if not args.refresh:
        return
    try:
        manifest = load_manifest()
        stale, _ = index_stale(manifest)
        if manifest.get("model") != args.model:
            stale = True
    except RuntimeError:
        stale = True
    if stale:
        do_build(args.model, full=False, quiet=True)


def cmd_query(args: argparse.Namespace) -> int:
    try:
        maybe_refresh(args)
        chunks, matrix, _ = load_index(args.model)

        ranked = rank_chunks(
            chunks,
            matrix,
            args.text,
            model=args.model,
            lexical_only=args.lexical_only,
            no_instruct=args.no_instruct,
        )
        picked = select_results(
            ranked,
            k=args.k,
            per_file=args.per_file,
            path_prefixes=args.path,
            relative_floor=args.relative_floor,
        )
    except RuntimeError as exc:
        # Si le service d'embedding est indisponible mais que l'index existe,
        # l'utilisateur peut explicitement retenter en lexical-only.
        print(f"ERROR: {exc}", file=sys.stderr)
        if not args.lexical_only:
            print("HINT: essaie `ctx.py query ... --lexical-only` si l'index existe.", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([
            {
                **asdict(item.chunk),
                "terms": None,  # évite de dumper le lexique dans la sortie agent
                "score": round(item.score, 4),
                "dense": round(item.dense, 4),
                "lexical": round(item.lexical, 4),
                "meta": round(item.meta, 4),
            }
            for item in picked
        ], indent=2, ensure_ascii=False))
        return 0

    if args.paths_only:
        for path in dict.fromkeys(item.chunk.path for item in picked):
            print(path)
        return 0

    width = max((len(f"{i.chunk.path}:{i.chunk.start}-{i.chunk.end}") for i in picked), default=0)
    for item in picked:
        c = item.chunk
        locator = f"{c.path}:{c.start}-{c.end}"
        if args.scores:
            suffix = (
                f"  [score={item.score:.3f} dense={item.dense:.3f} "
                f"lex={item.lexical:.3f}]"
            )
        else:
            suffix = ""
        print(f"{locator:<{width}}  {c.symbol}{suffix}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest()
    except RuntimeError as exc:
        print(str(exc))
        return 1

    stale, reason = index_stale(manifest)
    result = dict(manifest)
    result["stale"] = stale
    result["status"] = reason
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if stale else 0


def coverage_report() -> tuple[int, int, list[str]]:
    """Vérifie que chaque ligne non vide des fichiers indexés est couverte."""
    chunks = load_chunks()
    by_path: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        by_path.setdefault(chunk.path, []).append(chunk)

    total_nonempty = 0
    uncovered_nonempty = 0
    examples: list[str] = []
    for rel, file_chunks in by_path.items():
        path = ROOT / rel
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        covered: set[int] = set()
        for chunk in file_chunks:
            covered.update(range(chunk.start, chunk.end + 1))
        for lineno, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            total_nonempty += 1
            if lineno not in covered:
                uncovered_nonempty += 1
                if len(examples) < 10:
                    examples.append(f"{rel}:{lineno}")
    return total_nonempty, uncovered_nonempty, examples


def cmd_doctor(args: argparse.Namespace) -> int:
    problems: list[str] = []
    try:
        manifest = load_manifest()
        chunks = load_chunks()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2

    if manifest.get("model") != args.model:
        problems.append(
            f"modèle manifest={manifest.get('model')!r} != modèle demandé={args.model!r}"
        )
    cache = load_cache(str(manifest.get("model", args.model)))
    missing = sum(1 for chunk in chunks if chunk.key not in cache)
    if missing:
        problems.append(f"{missing} chunks sans vecteur")

    stale, reason = index_stale(manifest)
    if stale:
        problems.append(reason)

    total, uncovered, examples = coverage_report()
    if uncovered:
        problems.append(f"{uncovered}/{total} lignes non vides non couvertes ({', '.join(examples)})")

    duplicate_ranges = len(chunks) - len({(c.path, c.start, c.end, c.symbol) for c in chunks})
    if duplicate_ranges:
        problems.append(f"{duplicate_ranges} plages dupliquées")

    if problems:
        print("WARN")
        for problem in problems:
            print(f"- {problem}")
        return 1

    print(
        f"OK — {len(chunks)} chunks, {len({c.path for c in chunks})} fichiers, "
        f"couverture {total}/{total} lignes non vides."
    )
    return 0


# --------------------------------------------------------------------------- cli


def main() -> int:
    parser = argparse.ArgumentParser(prog="ctx", description=__doc__)
    parser.add_argument("--model", default=os.getenv("CTX_EMBED_MODEL", DEFAULT_MODEL))
    sub = parser.add_subparsers(dest="cmd", required=True)

    build = sub.add_parser("build", help="Construit / met à jour l'index.")
    build.add_argument("--full", action="store_true", help="Ignore le cache du modèle.")
    build.add_argument("--quiet", action="store_true", help="N'écrit rien si tout va bien.")
    build.set_defaults(func=cmd_build)

    query = sub.add_parser("query", help="Recherche hybride dans l'index.")
    query.add_argument("text")
    query.add_argument("-k", type=int, default=DEFAULT_K)
    query.add_argument("--per-file", type=int, default=DEFAULT_PER_FILE)
    query.add_argument("--path", action="append", help="Restreint à un préfixe de chemin.")
    query.add_argument("--scores", action="store_true")
    query.add_argument("--paths-only", action="store_true")
    query.add_argument("--json", action="store_true")
    query.add_argument("--no-instruct", action="store_true")
    query.add_argument(
        "--lexical-only",
        action="store_true",
        help="N'appelle pas le service d'embedding pour la requête.",
    )
    query.add_argument(
        "--refresh",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Met à jour silencieusement l'index s'il est obsolète (défaut: oui).",
    )
    query.add_argument(
        "--relative-floor",
        type=float,
        default=0.72,
        help="Après 3 résultats, coupe sous cette fraction du meilleur score; 0 désactive.",
    )
    query.set_defaults(func=cmd_query)

    status = sub.add_parser("status", help="État et fraîcheur de l'index.")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="Vérifie cache, modèle, fraîcheur et couverture.")
    doctor.set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    if getattr(args, "k", 1) < 1:
        parser.error("-k doit être >= 1")
    if getattr(args, "per_file", 1) < 1:
        parser.error("--per-file doit être >= 1")
    floor = getattr(args, "relative_floor", 0.0)
    if not 0.0 <= floor <= 1.0:
        parser.error("--relative-floor doit être entre 0 et 1")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())