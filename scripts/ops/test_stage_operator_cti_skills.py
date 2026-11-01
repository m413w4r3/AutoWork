#!/usr/bin/env python3
"""Offline smoke/regression checks for isolated CTI skill staging."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("stage_operator_cti_skills.py")
SPEC = importlib.util.spec_from_file_location("stage_operator_cti_skills", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class StageSkillsTests(unittest.TestCase):
    def test_stage_all_eight_with_exact_hash_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            hashes = module.stage(workspace)
            self.assertEqual(len(hashes), 9)  # Eight SKILL.md + index.
            bundle = workspace / ".agents" / "skills"
            manifest = json.loads((bundle / "manifest.json").read_text())
            self.assertEqual(manifest["sha256_by_skill"], hashes)
            for name in module.SKILL_NAMES:
                self.assertEqual(
                    (bundle / name / "SKILL.md").read_text().splitlines()[1],
                    f"name: {name}",
                )
            self.assertEqual(
                set(bundle.iterdir()) - {bundle / "manifest.json", bundle / "OPERATOR-CTI.md"},
                {bundle / name for name in module.SKILL_NAMES},
            )
            with self.assertRaises(FileExistsError):
                module.stage(workspace)

    def test_optional_claude_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            module.stage(workspace, claude=True)
            for name in module.SKILL_NAMES:
                link = workspace / ".claude" / "skills" / name
                self.assertTrue(link.is_symlink())
                self.assertTrue((link / "SKILL.md").is_file())

    def test_existing_bundle_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            existing = workspace / ".agents" / "skills"
            existing.mkdir(parents=True)
            guard = existing / "marker.txt"
            guard.write_text("keep")
            with self.assertRaises(FileExistsError):
                module.stage(workspace)
            self.assertEqual(guard.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
