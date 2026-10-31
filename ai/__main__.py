"""Run a real investigation without connecting the agent to Streamlit."""

import argparse
import json

from ai.agent import DEFAULT_TASK, investigate


def main():
    parser = argparse.ArgumentParser(description="FloodSense AI evidence investigation (not an official warning).")
    parser.add_argument("latitude", type=float)
    parser.add_argument("longitude", type=float)
    parser.add_argument("--task", default=DEFAULT_TASK)
    args = parser.parse_args()
    try:
        result = investigate(args.latitude, args.longitude, task=args.task)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0 if result["agent_status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
