"""Run the frozen text intent model plus local SoulAlign event gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import joblib

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.soulalign import decide


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--speech-text", required=True)
    parser.add_argument("--events-json", type=Path, required=True)
    args = parser.parse_args()
    bundle = joblib.load(args.model)
    features = bundle["vectorizer"].transform([args.speech_text])
    intent = str(bundle["classifier"].predict(features)[0])
    events = json.loads(args.events_json.read_text(encoding="utf-8"))
    result = decide(args.speech_text, events)
    result["intent_model_prediction"] = intent
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
