"""Live helper for recording recognition fixtures (NOT part of the offline suite).

Calls the C# execution server directly (bypassing the MCP adapter, which does not hot-reload), so a
fresh `analyze_model(brep)` can be captured right after a C# rebuild.

    python cad-planner/recognition/tests/swcall.py <tool> '<params json>' [out_file]

Prints the JSON response; with out_file, writes the first analysis item (the brep JSON) there.
"""
import json
import os
import sys
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, "..", "..", "..", "adapters", "claude")))

from execution_client import call_tool, get_state  # noqa: E402


def call(tool: str, params: dict) -> dict:
    return call_tool(tool, str(uuid.uuid4()), get_state(), params)


def main() -> int:
    tool, params = sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    resp = call(tool, params)
    out = sys.argv[3] if len(sys.argv) > 3 else None
    if out:
        items = (resp.get("cadState") or {}).get("features") or []
        if not items:
            sys.stderr.write(json.dumps(resp)[:2000] + "\n")
            return 1
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(json.loads(items[0]), fh, indent=1)
        sys.stderr.write("wrote %s\n" % out)
        return 0
    sys.stdout.write(json.dumps(resp, indent=1)[:6000] + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
