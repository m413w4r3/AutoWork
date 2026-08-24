"""Régression R66 : le mode lexical de ctx.py doit être un fallback autonome.

Ces tests exécutent ``ctx.py`` en subprocess dans un dépôt Git temporaire
minimal, sans jamais dépendre d'un fichier applicatif d'AutoWork.

Aucun credential d'embedding (BASE_URL / EMBEDDING_API_KEY) n'est requis
pour ``build --lexical-only`` ni pour ``query ... --lexical-only``, y
compris quand la requête déclenche un rebuild automatique.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CTX_SCRIPT = Path(__file__).resolve().parents[1] / "ctx.py"

OLD_OWNER_SOURCE = (
    "def old_owner_symbol():\n"
    "    \"\"\"Distinctive owner function, soon to be replaced.\"\"\"\n"
    "    return 1\n"
)

NEW_OWNER_SOURCE = (
    "def new_owner_symbol():\n"
    "    \"\"\"New distinctive owner function.\"\"\"\n"
    "    return 2\n"
)


def run_ctx(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Lance ctx.py dans `repo`, sans aucun credential d'embedding."""
    env = dict(os.environ)
    env.pop("BASE_URL", None)
    env.pop("EMBEDDING_API_KEY", None)
    return subprocess.run(
        ["uv", "run", str(CTX_SCRIPT), *args],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    return repo


def git_snapshot(repo: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "snapshot", "--allow-empty"], cwd=repo, check=True
    )


def test_fresh_lexical_build_without_credentials(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "src" / "old_owner.py").write_text(OLD_OWNER_SOURCE)
    git_snapshot(repo)

    build = run_ctx(repo, "build", "--lexical-only")
    assert build.returncode == 0, build.stderr

    query = run_ctx(repo, "query", "distinctive owner", "--lexical-only", "--paths-only")
    assert query.returncode == 0, query.stderr
    assert "src/old_owner.py" in query.stdout.splitlines()


def test_automatic_stale_refresh_without_manual_build(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "src" / "old_owner.py").write_text(OLD_OWNER_SOURCE)
    git_snapshot(repo)

    build = run_ctx(repo, "build", "--lexical-only")
    assert build.returncode == 0, build.stderr

    (repo / "src" / "old_owner.py").unlink()
    (repo / "src" / "new_owner.py").write_text(NEW_OWNER_SOURCE)
    git_snapshot(repo)

    # Aucun build manuel ici : --refresh est le défaut de `query`.
    query = run_ctx(repo, "query", "new distinctive owner", "--lexical-only", "--paths-only")
    assert query.returncode == 0, query.stderr
    paths = query.stdout.splitlines()
    assert "src/new_owner.py" in paths
    assert "src/old_owner.py" not in paths


def test_lexical_build_does_not_pretend_dense_is_ready(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "src" / "old_owner.py").write_text(OLD_OWNER_SOURCE)
    git_snapshot(repo)

    build = run_ctx(repo, "build", "--lexical-only")
    assert build.returncode == 0, build.stderr

    # Une query hybride normale doit échouer proprement : pas de credentials.
    dense_query = run_ctx(repo, "query", "distinctive owner")
    assert dense_query.returncode != 0
    assert "BASE_URL" in dense_query.stderr and "EMBEDDING_API_KEY" in dense_query.stderr

    # L'échec dense ne doit pas avoir corrompu l'index lexical.
    lexical_query = run_ctx(repo, "query", "distinctive owner", "--lexical-only", "--paths-only")
    assert lexical_query.returncode == 0, lexical_query.stderr
    assert "src/old_owner.py" in lexical_query.stdout.splitlines()
