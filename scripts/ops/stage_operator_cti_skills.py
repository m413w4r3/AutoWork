#!/usr/bin/env python3
"""Stage ONLY approved, generic CTI skills in a new isolated operator workspace.

No CTI evidence, old runs, repository runtime state or secrets are copied.
A staging operation refuses to replace an existing bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

SKILL_NAMES = (
    "autowork-cti-investigation-loop",
    "autowork-tool-strategy",
    "autowork-artifact-triage",
    "autowork-cti-pivot-strategy",
    "autowork-evidence-to-finding",
    "autowork-dotnet-analysis",
    "autowork-pe-native-analysis",
    "autowork-yara-bridge-workflow",
    "autowork-network-protocol-detection",
)


def stage(workspace: Path, *, claude: bool = False) -> dict[str, str]:
    repo = Path(__file__).resolve().parents[2]
    source = repo / ".agents" / "skills"
    if not workspace.is_dir() or workspace.is_symlink():
        raise ValueError("workspace must be an existing, non-symlink directory")

    # Never traverse a pre-existing workspace-level symlink during staging.
    for parent in (".agents", ".claude"):
        if (workspace / parent).is_symlink():
            raise ValueError(f"workspace {parent} must not be a symlink")

    target = workspace / ".agents" / "skills"
    if target.exists() or target.is_symlink():
        raise FileExistsError("skills bundle already exists; use a new session workspace")
    if claude and ((workspace / ".claude" / "skills").exists() or (workspace / ".claude" / "skills").is_symlink()):
        raise FileExistsError("Claude skills directory already exists; refusing overwrite")

    # Resolve the fixed allowlist before making any destination directory.
    original: dict[str, bytes] = {}
    for name in SKILL_NAMES:
        skill_file = source / name / "SKILL.md"
        if not skill_file.is_file() or skill_file.is_symlink():
            raise ValueError(f"approved skill missing or symlinked: {name}")
        content = skill_file.read_bytes()
        if not content.startswith((f"---\nname: {name}\n").encode()):
            raise ValueError(f"skill frontmatter mismatch: {name}")
        original[name] = content

    target.mkdir(parents=True, exist_ok=False)
    hashes: dict[str, str] = {}
    try:
        for name, content in original.items():
            directory = target / name
            directory.mkdir()
            (directory / "SKILL.md").write_bytes(content)
            hashes[name] = hashlib.sha256(content).hexdigest()

        index = source / "OPERATOR-CTI.md"
        if not index.is_file() or index.is_symlink():
            raise ValueError("operator CTI skills index missing")
        shutil.copyfile(index, target / "OPERATOR-CTI.md")
        hashes["OPERATOR-CTI.md"] = hashlib.sha256(index.read_bytes()).hexdigest()
        (target / "manifest.json").write_text(
            json.dumps({"sha256_by_skill": hashes}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        if claude:
            links = workspace / ".claude" / "skills"
            links.mkdir(parents=True, exist_ok=False)
            for name in SKILL_NAMES:
                (links / name).symlink_to(Path("..") / ".." / ".agents" / "skills" / name, target_is_directory=True)
    except BaseException:
        shutil.rmtree(target)
        # An incomplete Claude directory is not a usable installation.
        if claude:
            shutil.rmtree(workspace / ".claude" / "skills", ignore_errors=True)
        raise
    return hashes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--claude", action="store_true", help="also expose safe links for Claude Code")
    args = parser.parse_args()
    hashes = stage(args.workspace, claude=args.claude)
    print(f"Staged {len(hashes) - 1} CTI skills and index into {args.workspace}")


if __name__ == "__main__":
    main()
