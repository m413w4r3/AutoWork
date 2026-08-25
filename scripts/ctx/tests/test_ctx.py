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
import sys
from pathlib import Path

import pytest

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


def run_ctx(repo: Path, *args: str, stdlib_only: bool = False) -> subprocess.CompletedProcess[str]:
    """Lance ctx.py dans `repo`, sans aucun credential d'embedding."""
    env = dict(os.environ)
    env.pop("BASE_URL", None)
    env.pop("EMBEDDING_API_KEY", None)
    return subprocess.run(
        ([sys.executable, "-S"] if stdlib_only else [sys.executable])
        + [str(CTX_SCRIPT), *args],
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


def test_fresh_lexical_build_stdlib_only(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "src" / "old_owner.py").write_text(OLD_OWNER_SOURCE)
    git_snapshot(repo)

    build = run_ctx(repo, "build", "--lexical-only", stdlib_only=True)
    assert build.returncode == 0, build.stderr

    query = run_ctx(repo, "query", "distinctive owner", "--lexical-only", "--paths-only", stdlib_only=True)
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

    # AUTO retombe automatiquement sur le lexical sans credentials.
    dense_query = run_ctx(repo, "query", "distinctive owner", "--paths-only")
    assert dense_query.returncode == 0, dense_query.stderr
    assert "src/old_owner.py" in dense_query.stdout.splitlines()

    hybrid_query = run_ctx(repo, "query", "distinctive owner", "--hybrid-only")
    assert hybrid_query.returncode != 0
    assert (
        "credentials" in hybrid_query.stderr
        or "BASE_URL" in hybrid_query.stderr
        or "dépendance dense" in hybrid_query.stderr
    )

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


def test_auto_falls_back_without_site_packages(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "src" / "stdlib_only.py").write_text("def stdlib_only_locator():\n    return True\n")
    git_snapshot(repo)

    result = run_ctx(repo, "query", "stdlib only locator", "--paths-only", stdlib_only=True)
    assert result.returncode == 0, result.stderr
    assert "src/stdlib_only.py" in result.stdout.splitlines()
    assert "fallback lexical" in result.stderr


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

    build = run_ctx(repo, "build", "--lexical-only")
    assert build.returncode == 0, build.stderr

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

dummy_vectors = {{
    "old_key_1": np.random.randn(768).astype(np.float32),
    "old_key_2": np.random.randn(768).astype(np.float32),
}}

save_cache(model, dummy_vectors)

cache_dir_path = cache_dir(model)
vec_path = cache_dir_path / "vectors.npy"
key_path = cache_dir_path / "keys.json"
meta_path = cache_dir_path / "meta.json"

assert vec_path.exists(), f"vectors.npy should exist at {{vec_path}}"
assert key_path.exists(), f"keys.json should exist at {{key_path}}"
assert meta_path.exists(), f"meta.json should exist at {{meta_path}}"

loaded = load_cache(model)
assert len(loaded) == 2, f"should have 2 vectors, got {{len(loaded)}}"

save_cache(model, {{}})

assert not vec_path.exists(), "vectors.npy should be removed"
assert not key_path.exists(), "keys.json should be removed"
assert not meta_path.exists(), "meta.json should be removed"

loaded_empty = load_cache(model)
assert loaded_empty == {{}}, f"load_cache should return empty dict, got {{loaded_empty}}"

print("OK")
'''
    )

    result = subprocess.run(
        [sys.executable, str(test_script)],
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
# Le scorer lexical s'exécute directement avec l'interpréteur du test. Les
# tests de cache dense chargent numpy uniquement dans leur script auxiliaire,
# sans l'imposer au démarrage de ctx.py.


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
        [sys.executable, "-S", str(script)],
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
    if not (REPO_ROOT / "refacto_baseLine" / "ctx_benchmark.json").exists():
        pytest.skip("benchmark fixture absent from this checkout")
    env = dict(os.environ)
    env.pop("BASE_URL", None)
    env.pop("EMBEDDING_API_KEY", None)

    build = subprocess.run(
        [sys.executable, str(CTX_SCRIPT), "build", "--lexical-only"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert build.returncode == 0, build.stderr

    run = subprocess.run(
        [sys.executable, str(BENCHMARK_SCRIPT), "--lexical-only", "--json", "--no-check"],
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
    if not (REPO_ROOT / "refacto_baseLine" / "ctx_benchmark.json").exists():
        pytest.skip("benchmark fixture absent from this checkout")
    env = dict(os.environ)
    env.pop("BASE_URL", None)
    env.pop("EMBEDDING_API_KEY", None)

    checked = subprocess.run(
        [sys.executable, str(BENCHMARK_SCRIPT), "--lexical-only", "--json"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    unchecked = subprocess.run(
        [sys.executable, str(BENCHMARK_SCRIPT), "--lexical-only", "--json", "--no-check"],
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


# --------------------------------------------------------------------------- R75 : unités du scorer
#
# Ces tests exercent directement les primitives de ranking de ctx.py
# (`lexical_scores`, `meta_scores`, `rank_chunks`, `path_role`) sur des
# `Chunk` SYNTHÉTIQUES. Ils ne dépendent d'aucun fichier d'AutoWork, d'aucune
# requête du benchmark R67 et d'aucun index construit : ils décrivent des
# propriétés générales du scorer, valables pour n'importe quel dépôt.
#
# Ces assertions sont lancées directement avec l'interpréteur courant : elles
# vérifient que le scorer lexical reste disponible sans bootstrap dense.

SCORER_PRELUDE = '''
import sys
sys.path.insert(0, {ctx_dir!r})
import ctx


def chunk(path, symbol, body, kind="function", start=1, end=20):
    """Construit un Chunk synthétique avec le même lexique qu'un vrai build."""
    return ctx.Chunk(
        path=path,
        start=start,
        end=end,
        symbol=symbol,
        kind=kind,
        key=path + symbol,
        terms=ctx.lexical_terms(path, symbol, body),
    )


def rank(chunks, query):
    """Ordre lexical-only : renvoie les chunks du meilleur au moins bon."""
    ranked = ctx.rank_chunks(
        chunks,
        [],
        query,
        model="unused",
        lexical_only=True,
        no_instruct=True,
    )
    return [item.chunk for item in ranked]
'''


def run_scorer(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    """Exécute des assertions de scoring dans un interpréteur stdlib-only."""
    script = tmp_path / "scorer_case.py"
    script.write_text(
        SCORER_PRELUDE.format(ctx_dir=str(CTX_SCRIPT.parent)) + body + '\nprint("OK")\n'
    )
    return subprocess.run(
        [sys.executable, "-S", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )


def assert_scorer_ok(tmp_path: Path, body: str) -> None:
    result = run_scorer(tmp_path, body)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "OK" in result.stdout, result.stdout


def test_path_role_uses_generic_conventions_not_repo_prefixes(tmp_path: Path) -> None:
    """Le rôle d'un fichier vient de conventions universelles, pas d'une liste
    de préfixes propres à un dépôt : le même classement doit valoir pour des
    arborescences arbitraires.
    """
    assert_scorer_ok(
        tmp_path,
        '''
# Tests : répertoire dédié OU convention de nommage, à n'importe quelle profondeur.
for path in (
    "tests/test_thing.py",
    "any/nested/tests/helpers.py",
    "pkg/__tests__/widget.ts",
    "svc/spec/behaviour.rb",
    "web/e2e/checkout.spec.ts",
    "pkg/test_widget.py",
    "pkg/widget_test.go",
    "web/widget.test.tsx",
    "pkg/conftest.py",
):
    assert ctx.path_role(path) == "test", path

for path in ("docs/guide.md", "README.md", "notes/adr/0001-choice.md", "docs/deep/topic.rst"):
    assert ctx.path_role(path) == "doc", path

for path in ("db/migrations/0001_init.py", "alembic/versions/abc.py"):
    assert ctx.path_role(path) == "migration", path

for path in ("src/service.py", "lib/widget.ts", "cmd/main.go", "app/models.py"):
    assert ctx.path_role(path) == "source", path

# Le code source garde le prior maximal ; les artefacts descriptifs perdent
# les égalités sans être exclus.
assert ctx.path_prior("src/service.py") == 1.0
assert ctx.path_prior("tests/test_service.py") < 1.0
assert ctx.path_prior("docs/guide.md") < ctx.path_prior("tests/test_service.py")
assert 0.0 < ctx.path_prior("db/migrations/0001_init.py") < 1.0
''',
    )


def test_focused_symbol_beats_prose_symbol_covering_same_concepts(tmp_path: Path) -> None:
    """Un symbole dont la requête est l'essentiel bat un nom-phrase qui ne fait
    que la mentionner. `release_conversation` et
    `test_release_conversation_success_with_keep_policy` couvrent les mêmes
    concepts ; seul le premier est un locator.
    """
    assert_scorer_ok(
        tmp_path,
        '''
implementation = chunk(
    "src/routes_conversations.py",
    "ConversationRoutes.release_conversation",
    "def release_conversation(self, handle):\\n    return self.registry.release(handle)\\n",
)
prose = chunk(
    "src/describe.py",  # même rôle "source" : on isole l'effet du symbole
    "check_release_conversation_success_with_keep_policy_and_retry",
    "def check_release_conversation_success_with_keep_policy_and_retry():\\n    pass\\n",
)
order = rank([prose, implementation], "release conversation")
assert order[0] is implementation, [c.symbol for c in order]
''',
    )


def test_long_chunk_does_not_win_by_vocabulary_accumulation(tmp_path: Path) -> None:
    """Le lexique d'un chunk est un set : sans normalisation de longueur, un
    chunk long matche mécaniquement plus de termes qu'un chunk court sans être
    plus pertinent. Un gros module fourre-tout ne doit pas battre la fonction
    qui porte réellement le concept.
    """
    assert_scorer_ok(
        tmp_path,
        '''
filler = " ".join("filler%d unrelated%d noise%d" % (i, i, i) for i in range(200))

# Contient TOUS les termes de la requête, noyés dans un vocabulaire énorme.
grab_bag = chunk(
    "src/kitchen_sink.py",
    "<module>",
    "quota throttle budget enforcement " + filler,
    kind="module",
    start=1,
    end=110,
)
# N'en contient qu'une partie, mais c'est son sujet.
focused = chunk(
    "src/limits.py",
    "enforce_quota_throttle",
    "def enforce_quota_throttle(budget):\\n    return budget\\n",
)
order = rank([grab_bag, focused], "quota throttle budget enforcement")
assert order[0] is focused, [c.symbol for c in order]
''',
    )


def test_path_concept_plus_symbol_entity_beats_generic_word_match(tmp_path: Path) -> None:
    """Un chunk dont le CHEMIN porte un concept demandé ("repository") et dont
    le SYMBOLE porte l'entité demandée doit battre un chunk qui ne matche qu'un
    mot générique de la requête.
    """
    assert_scorer_ok(
        tmp_path,
        '''
owner = chunk(
    "src/infrastructure/database/repositories/model_conversations.py",
    "SqlAlchemyModelConversationRepository",
    "class SqlAlchemyModelConversationRepository:\\n    def save(self, row):\\n        ...\\n",
    kind="class",
)
generic = chunk(
    "src/util/helpers.py",
    "load_state",
    "def load_state(payload):\\n    # generic state handling\\n    return payload\\n",
)
order = rank([generic, owner], "persist and reload model conversation state database repository")
assert order[0] is owner, [c.path for c in order]
''',
    )


def test_generic_one_token_symbol_gets_no_citation_bonus(tmp_path: Path) -> None:
    """`symbol in query` vise le cas "la requête cite ce symbole". Un nom d'un
    seul token générique (Extension, Config...) qui se trouve être un sous-mot
    de la requête n'est pas une citation : sans garde-fou, le symbole le moins
    discriminant du dépôt rafle le plus gros bonus meta.
    """
    assert_scorer_ok(
        tmp_path,
        '''
query = "browser extension server request conversation routing"

generic = chunk("src/soak.py", "Extension", "class Extension:\\n    pass\\n", kind="class")
meta = ctx.meta_scores([generic], query)[0]
# Sans le garde-fou, ce seul chunk encaissait le bonus de citation (0.55).
assert meta < 0.55, meta

# Une vraie citation multi-token garde son bonus.
real = chunk(
    "src/routing.py",
    "conversation_routing",
    "def conversation_routing():\\n    pass\\n",
)
assert ctx.meta_scores([real], query)[0] > meta
''',
    )


def test_exact_symbol_match_stays_strongly_prioritised(tmp_path: Path) -> None:
    """Garantie non négociable : chercher un identifiant exact doit ramener sa
    définition en tête, y compris face à des chunks qui le mentionnent.
    """
    assert_scorer_ok(
        tmp_path,
        '''
definition = chunk(
    "src/policy.py",
    "ConversationPolicy",
    "class ConversationPolicy:\\n    keep = True\\n",
    kind="class",
)
mention = chunk(
    "src/consumer.py",
    "build_pipeline",
    "def build_pipeline():\\n    # uses ConversationPolicy under the hood\\n"
    "    return ConversationPolicy()\\n",
)
doc = chunk("docs/policies.md", "Conversation policy", "The ConversationPolicy decides retention.\\n", kind="doc")

order = rank([mention, doc, definition], "ConversationPolicy")
assert order[0] is definition, [c.symbol for c in order]
''',
    )


def test_doc_and_migration_do_not_outrank_relevant_source(tmp_path: Path) -> None:
    """Docs et migrations restent indexées et retournables, mais ne battent pas
    arbitrairement le code source qui implémente le comportement demandé.
    """
    assert_scorer_ok(
        tmp_path,
        '''
source = chunk(
    "src/thermal.py",
    "stabilize_pressure_valve",
    "def stabilize_pressure_valve():\\n    return adjust_driver_output()\\n",
)
adr = chunk(
    "docs/adr/0002-hardware.md",
    "Stabilize the pressure valve at the driver level",
    "We chose to stabilize the pressure valve at the driver level; the pressure"
    " valve logic stays out of hardware.\\n",
    kind="doc",
)
migration = chunk(
    "db/migrations/0007_pressure_valve.py",
    "upgrade",
    "def upgrade():\\n    op.add_column('valve', sa.Column('stabilize_pressure', sa.Boolean))\\n",
)

order = rank([adr, migration, source], "stabilize pressure valve")
assert order[0] is source, [c.path for c in order]

# ...mais ils restent présents dans le classement, pas filtrés.
assert set(c.path for c in order) == {"src/thermal.py", "docs/adr/0002-hardware.md", "db/migrations/0007_pressure_valve.py"}
''',
    )


def test_scorer_works_with_zero_embeddings(tmp_path: Path) -> None:
    """Lexical-only reste fonctionnel avec zéro embedding : `rank_chunks` ne
    doit jamais toucher la matrice dense, même vide.
    """
    assert_scorer_ok(
        tmp_path,
        '''
chunks = [
    chunk("src/a.py", "alpha_handler", "def alpha_handler():\\n    pass\\n"),
    chunk("src/b.py", "beta_handler", "def beta_handler():\\n    pass\\n"),
]
ranked = ctx.rank_chunks(
    chunks,
    [],
    "alpha handler",
    model="unused",
    lexical_only=True,
    no_instruct=True,
)
assert len(ranked) == 2
assert all(item.dense == 0.0 for item in ranked)
assert ranked[0].chunk.symbol == "alpha_handler"
assert ranked[0].score > 0.0

# Une requête sans terme exploitable ne doit pas exploser.
empty = ctx.rank_chunks(
    chunks,
    [],
    "   ",
    model="unused",
    lexical_only=True,
    no_instruct=True,
)
assert len(empty) == 2
''',
    )
