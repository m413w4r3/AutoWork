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


def test_exact_symbol_beats_narrative_test_mention(tmp_path: Path) -> None:
    """R68: un symbole source exact doit battre une mention narrative du même
    concept dans un test, même quand le test couvre plus de tokens de la
    requête que le symbole.
    """
    repo = init_repo(tmp_path)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "tests").mkdir(exist_ok=True)

    (repo / "src" / "write_path.py").write_text(
        "def stage_and_flush_pending() -> None:\n"
        "    \"\"\"Move a queued record onto the write path, then push it out"
        " immediately.\"\"\"\n"
        "    return None\n"
    )
    (repo / "tests" / "test_flow_details.py").write_text(
        "def test_flow_details() -> None:\n"
        "    \"\"\"Stage a pending ledger entry, then flush it: full stage,"
        " pending, ledger, entry, flush cycle end to end.\"\"\"\n"
        "    assert True\n"
    )
    git_snapshot(repo)

    build = run_ctx(repo, "build", "--lexical-only")
    assert build.returncode == 0, build.stderr

    query = run_ctx(
        repo, "query", "stage flush pending ledger entry", "--lexical-only", "--paths-only", "-k", "2"
    )
    assert query.returncode == 0, query.stderr
    paths = query.stdout.splitlines()
    assert paths, "expected at least one result"
    assert paths[0] == "src/write_path.py", paths


def test_source_implementation_beats_adr_narrative(tmp_path: Path) -> None:
    """R68: une implémentation source doit battre un ADR/document qui répète
    les mêmes mots sans implémenter le comportement.
    """
    repo = init_repo(tmp_path)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "docs" / "adr").mkdir(parents=True)

    (repo / "src" / "thermal_control.py").write_text(
        "def stabilize_pressure_valve() -> None:\n"
        "    \"\"\"Adjust the driver output so downstream load remains"
        " steady.\"\"\"\n"
        "    return None\n"
    )
    (repo / "docs" / "adr" / "0002-hardware-choice.md").write_text(
        "# Context\n\n"
        "We evaluated several approaches for regulator behavior under"
        " load.\n\n"
        "## Decision\n\n"
        "The system must stabilize the pressure valve whenever inlet flow"
        " spikes; this note documents why we chose to stabilize the"
        " pressure valve at the driver level, keeping the pressure valve"
        " logic out of hardware.\n"
    )
    git_snapshot(repo)

    build = run_ctx(repo, "build", "--lexical-only")
    assert build.returncode == 0, build.stderr

    query = run_ctx(
        repo, "query", "stabilize pressure valve", "--lexical-only", "--paths-only", "-k", "2"
    )
    assert query.returncode == 0, query.stderr
    paths = query.stdout.splitlines()
    assert paths, "expected at least one result"
    assert paths[0] == "src/thermal_control.py", paths


def test_explicit_test_oriented_query_still_returns_test(tmp_path: Path) -> None:
    """R68: une requête explicitement orientée test doit encore retrouver le
    test concerné — le ranking field-aware ne doit pas éliminer les tests.
    """
    repo = init_repo(tmp_path)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "tests").mkdir(exist_ok=True)

    (repo / "src" / "unrelated_module.py").write_text(
        "def unrelated_helper() -> None:\n"
        "    \"\"\"Does something else entirely.\"\"\"\n"
        "    return None\n"
    )
    (repo / "tests" / "test_flow_details.py").write_text(
        "def test_flow_details() -> None:\n"
        "    \"\"\"Covers the flow details edge case behavior.\"\"\"\n"
        "    assert True\n"
    )
    git_snapshot(repo)

    build = run_ctx(repo, "build", "--lexical-only")
    assert build.returncode == 0, build.stderr

    query = run_ctx(
        repo, "query", "test flow details behavior", "--lexical-only", "--paths-only", "-k", "2"
    )
    assert query.returncode == 0, query.stderr
    paths = query.stdout.splitlines()
    assert "tests/test_flow_details.py" in paths, paths


def test_empty_cache_file_cleanup(tmp_path: Path) -> None:
    """R66a: Vérifier que save_cache({}) supprime les fichiers cache obsolètes."""
    repo = init_repo(tmp_path)
    (repo / "src" / "old_owner.py").write_text(OLD_OWNER_SOURCE)
    git_snapshot(repo)

    # Lance un build lexical pour initialiser l'index.
    build = run_ctx(repo, "build", "--lexical-only")
    assert build.returncode == 0, build.stderr

    # Crée un script de test qui s'exécute dans le contexte du repo.
    test_script = repo / ".test_cache_cleanup.py"
    test_script.write_text(
        f'''
import json
import sys
from pathlib import Path
import numpy as np

sys.path.insert(0, {repr(str(CTX_SCRIPT.parent))})
from ctx import cache_dir, load_cache, save_cache

model = "test-model-empty-cleanup"

# Crée un cache avec du dummy data.
dummy_vectors = {{
    "old_key_1": np.random.randn(768).astype(np.float32),
    "old_key_2": np.random.randn(768).astype(np.float32),
}}

# Sauvegarde le cache.
save_cache(model, dummy_vectors)

# Vérifie que les fichiers existent.
cache_dir_path = cache_dir(model)
vec_path = cache_dir_path / "vectors.npy"
key_path = cache_dir_path / "keys.json"
meta_path = cache_dir_path / "meta.json"

assert vec_path.exists(), f"vectors.npy should exist at {{vec_path}}"
assert key_path.exists(), f"keys.json should exist at {{key_path}}"
assert meta_path.exists(), f"meta.json should exist at {{meta_path}}"

# Vérifie que load_cache retrouve les données.
loaded = load_cache(model)
assert len(loaded) == 2, f"should have 2 vectors, got {{len(loaded)}}"

# Appelle save_cache avec un cache vide.
save_cache(model, {{}})

# Vérifie que les fichiers ont disparu.
assert not vec_path.exists(), "vectors.npy should be removed"
assert not key_path.exists(), "keys.json should be removed"
assert not meta_path.exists(), "meta.json should be removed"

# Vérifie que load_cache retourne une dict vide.
loaded_empty = load_cache(model)
assert loaded_empty == {{}}, f"load_cache should return empty dict, got {{loaded_empty}}"

print("OK")
'''
    )

    result = subprocess.run(
        ["uv", "run", "--python", "3.12", "--with", "openai", "--with", "numpy", str(test_script)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, f"test script failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK" in result.stdout, f"test script did not print OK. stdout: {result.stdout}"
