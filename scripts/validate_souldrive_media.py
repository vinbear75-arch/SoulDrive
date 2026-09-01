"""Validate real media references before training or competition submission."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.multimodal_media import validate_episode_media


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--require-media", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    errors = []
    episodes = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines()]
    for episode in episodes:
        errors.extend(validate_episode_media(episode, args.media_root.resolve(), require_media=args.require_media))
    result = {
        "dataset": str(args.dataset),
        "media_root": str(args.media_root.resolve()),
        "episode_count": len(episodes),
        "require_media": args.require_media,
        "valid": not errors,
        "error_count": len(errors),
        "errors": errors[:200],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
