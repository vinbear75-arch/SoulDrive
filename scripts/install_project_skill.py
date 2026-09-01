"""Download a GitHub Skill into a SoulDrive project's review inbox.

This command never executes downloaded scripts.  Use ``--approve`` only after
reviewing the downloaded SKILL.md and its bundled files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.project_skill_installer import (  # noqa: E402
    SkillInstallError,
    approve_skill_from_inbox,
    install_skill_to_inbox,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install a project Skill from GitHub into the SoulDrive review inbox")
    parser.add_argument("--workspace", required=True, help="SoulDrive workspace directory")
    parser.add_argument("--source", required=True, help="GitHub URL, raw URL, or owner/repository/path")
    parser.add_argument("--skill-name", default=None, help="Optional local Skill directory name")
    parser.add_argument("--approve", action="store_true", help="Move the downloaded Skill from inbox into active project skills after review")
    args = parser.parse_args()

    try:
        result = install_skill_to_inbox(str(Path(args.workspace).resolve() / "skills"), args.source, args.skill_name)
        if args.approve:
            result["approval"] = approve_skill_from_inbox(str(Path(args.workspace).resolve() / "skills"), result["skill_name"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except SkillInstallError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
