"""LIVE fixture recorder (needs SolidWorks + the execution server; not part of the offline suite).

Imports every STEP in fixtures/steps/ and writes its analyze_model(brep) dump to fixtures/<name>.brep.json.
How the STEPs themselves were built is documented in record_fixtures.md.

    python cad-planner/recognition/tests/record_fixtures.py [name ...]
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import swcall  # noqa: E402

_STEPS = os.path.join(_HERE, "fixtures", "steps")


def record(name: str) -> dict:
    swcall.call("close_document", {"close_all": True})
    # Native (backslash) path: LoadFile4 rejects forward slashes for STEP on SW 2025.
    opened = swcall.call("open_document", {"file_path": os.path.join(_STEPS, name + ".step")})
    if opened.get("status") != "COMPLETED":
        raise RuntimeError("%s: %s" % (name, opened.get("error")))
    resp = swcall.call("analyze_model", {"analysis_type": "brep"})
    brep = json.loads(resp["cadState"]["features"][0])
    with open(os.path.join(_HERE, "fixtures", name + ".brep.json"), "w", encoding="utf-8") as fh:
        json.dump(brep, fh, indent=1)
    swcall.call("close_document", {"close_all": True})
    return brep


def main() -> int:
    names = sys.argv[1:] or sorted(f[:-5] for f in os.listdir(_STEPS) if f.endswith(".step"))
    for n in names:
        b = record(n)
        sys.stdout.write("%s: bodies=%s faces=%d edges=%d\n"
                         % (n, b.get("body_count"), len(b.get("faces", [])), len(b.get("edges", []))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
