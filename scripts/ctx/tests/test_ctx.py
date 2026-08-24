"""Régression R66 : le mode lexical de ctx.py doit être un fallback autonome.

Ces tests exécutent ``ctx.py`` en subprocess dans un dépôt Git temporaire
minimal, sans jamais dépendre d'un fichier applicatif d'AutoWork.

Aucun credential d'embedding (BASE_URL / EMBEDDING_API_KEY) n'est requis
pour ``build --lexical-only`` ni pour ``query ... --lexical-only``, y
compris quand la requête déclenche un rebuild automatique.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CTX_SCRIPT = Path(__file__).resolve().parents[1] / "ctx.py"
BENCHMARK_SCRIPT = Path(__file__).resolve().parents[1] / "benchmark.py"
REPO_ROOT = Path(__file__).resolve().parents[3]

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


# --------------------------------------------------------------------------- R73 : scripts/ctx/benchmark.py
#
# `benchmark.py` réutilise les primitives de `ctx.py` pour rejouer le
# benchmark de navigation gelé R67 (cf. refacto_baseLine/R67_benchmark_spec.md)
# et calculer ses métriques mécaniquement. Les tests ci-dessous vérifient le
# calcul (score_query/aggregate) sur des `Chunk`/`Ranked` synthétiques —
# aucun rang n'est codé en dur, seul l'arithmétique est fixée — ainsi que la
# forme et la cohérence de la sortie CLI sur les 12 requêtes réellement
# gelées, qui pointent nécessairement vers des fichiers d'AutoWork.
#
# `benchmark.py` importe `ctx.py`, qui dépend de numpy/openai au niveau
# module : comme les autres tests de ce fichier tournent via
# `uv run --python 3.12 --with pytest pytest ...` (sans ces deps dans
# l'environnement pytest lui-même), les tests unitaires de scoring
# s'exécutent dans un sous-processus `uv run --with openai --with numpy`,
# exactement comme `test_empty_cache_file_cleanup` ci-dessus.


def run_benchmark_scoring_script(repo: Path, body: str) -> subprocess.CompletedProcess[str]:
    script = repo / ".test_benchmark_scoring.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {repr(str(BENCHMARK_SCRIPT.parent))})\n"
        "import benchmark\n"
        "import ctx as ctx_module\n\n"
        "def _ranked(path, start, end):\n"
        "    chunk = ctx_module.Chunk(\n"
        "        path=path, start=start, end=end, symbol='sym', kind='function',\n"
        "        key=path, terms=(),\n"
        "    )\n"
        "    return ctx_module.Ranked(chunk=chunk, dense=0.0, lexical=0.0, meta=0.0, score=1.0)\n\n"
        f"{body}\n"
        "print('OK')\n"
    )
    return subprocess.run(
        ["uv", "run", "--python", "3.12", "--with", "openai", "--with", "numpy", str(script)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_score_query_hit_at_first_rank(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    result = run_benchmark_scoring_script(
        repo,
        "spec = {'id': 'T1', 'text': 'irrelevant', 'owners': ('owner.py',)}\n"
        "picked = [_ranked('owner.py', 1, 10), _ranked('other.py', 1, 5)]\n"
        "r = benchmark.score_query(spec, picked)\n"
        "assert r.first_relevant_rank == 1\n"
        "assert r.top3 and r.top8\n"
        "assert r.files_before_first_hit == 1\n"
        "assert r.lines_before_first_hit == 10\n",
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK" in result.stdout


def test_score_query_hit_outside_top3_counts_all_files_before_hit(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    result = run_benchmark_scoring_script(
        repo,
        "spec = {'id': 'T2', 'text': 'irrelevant', 'owners': ('owner.py',)}\n"
        "picked = [\n"
        "    _ranked('a.py', 1, 5), _ranked('b.py', 1, 5),\n"
        "    _ranked('c.py', 1, 5), _ranked('owner.py', 1, 20),\n"
        "]\n"
        "r = benchmark.score_query(spec, picked)\n"
        "assert r.first_relevant_rank == 4\n"
        "assert not r.top3\n"
        "assert r.top8\n"
        "assert r.files_before_first_hit == 4\n"
        "assert r.lines_before_first_hit == 5 + 5 + 5 + 20\n",
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK" in result.stdout


def test_score_query_miss_uses_full_topk(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    result = run_benchmark_scoring_script(
        repo,
        "spec = {'id': 'T3', 'text': 'irrelevant', 'owners': ('owner.py',)}\n"
        "picked = [_ranked('a.py', 1, 5), _ranked('b.py', 1, 7), _ranked('a.py', 8, 12)]\n"
        "r = benchmark.score_query(spec, picked)\n"
        "assert r.first_relevant_rank is None\n"
        "assert not r.top3\n"
        "assert not r.top8\n"
        "# MISS : fichiers distincts / lignes sur tout le top-k retourné (a.py compte une fois).\n"
        "assert r.files_before_first_hit == 2\n"
        "assert r.lines_before_first_hit == 5 + 7 + 5\n",
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK" in result.stdout


def test_aggregate_excludes_miss_from_rank_stats_but_not_hit_rates(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    result = run_benchmark_scoring_script(
        repo,
        "hit = benchmark.score_query({'id': 'H', 'text': 'x', 'owners': ('owner.py',)}, [_ranked('owner.py', 1, 4)])\n"
        "miss = benchmark.score_query({'id': 'M', 'text': 'x', 'owners': ('owner.py',)}, [_ranked('a.py', 1, 4)])\n"
        "agg = benchmark.aggregate([hit, miss])\n"
        "assert agg['n_queries'] == 2\n"
        "assert agg['top3_hit_rate'] == 0.5\n"
        "assert agg['top8_hit_rate'] == 0.5\n"
        "assert agg['n_miss'] == 1\n"
        "# Le MISS ne compte pas dans la statistique de rang (spec R67), donc la\n"
        "# médiane/moyenne de rang ne reflète que le hit.\n"
        "assert agg['median_first_relevant_rank'] == 1\n"
        "assert agg['mean_first_relevant_rank'] == 1\n",
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK" in result.stdout


def test_benchmark_cli_runs_frozen_12_queries_lexical_only() -> None:
    """Bout-en-bout contre l'index courant du dépôt : la ground truth du
    benchmark gelé pointe par nature vers des fichiers réels d'AutoWork, donc
    ce test (contrairement aux autres de ce fichier) s'exécute dans le vrai
    dépôt plutôt que dans un dépôt synthétique."""
    env = dict(os.environ)
    env.pop("BASE_URL", None)
    env.pop("EMBEDDING_API_KEY", None)

    build = subprocess.run(
        ["uv", "run", str(CTX_SCRIPT), "build", "--lexical-only"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert build.returncode == 0, build.stderr

    run = subprocess.run(
        ["uv", "run", str(BENCHMARK_SCRIPT), "--lexical-only", "--json", "--no-check"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)

    assert [q["id"] for q in payload["queries"]] == [f"Q{i}" for i in range(1, 13)]

    for q in payload["queries"]:
        rank = q["first_relevant_rank"]
        # Aucun rang n'est prédit ici : seule la cohérence mécanique entre le
        # rang calculé et les booléens top3/top8/MISS qui en dérivent est
        # vérifiée.
        if rank is None:
            assert q["top3"] is False and q["top8"] is False
        else:
            assert 1 <= rank <= 8
            assert q["top3"] == (rank <= 3)
            assert q["top8"] is True
        assert q["files_before_first_hit"] >= 1
        assert q["lines_before_first_hit"] >= 1

    agg = payload["aggregate"]
    assert agg["n_queries"] == 12
    assert 0.0 <= agg["top3_hit_rate"] <= 1.0
    assert 0.0 <= agg["top8_hit_rate"] <= 1.0
    assert len(payload["verdicts"]) == 3


def test_benchmark_exit_code_reflects_thresholds() -> None:
    env = dict(os.environ)
    env.pop("BASE_URL", None)
    env.pop("EMBEDDING_API_KEY", None)

    checked = subprocess.run(
        ["uv", "run", str(BENCHMARK_SCRIPT), "--lexical-only", "--json"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    unchecked = subprocess.run(
        ["uv", "run", str(BENCHMARK_SCRIPT), "--lexical-only", "--json", "--no-check"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )

    assert unchecked.returncode == 0, unchecked.stderr
    assert checked.returncode in (0, 1), checked.stderr

    payload = json.loads(checked.stdout)
    all_pass = all(payload["verdicts"].values())
    assert checked.returncode == (0 if all_pass else 1)
