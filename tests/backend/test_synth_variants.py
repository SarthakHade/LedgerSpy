"""P5: the scenario guarantees hold for other seeds, scales and periods, not only for the committed reference.

Each variant is generated into a temporary folder; the per-scenario data checks of `test_synth_scenarios.py`
then run against it in a fresh interpreter (those modules load their dataset at import time).
"""

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from ledgerspy.synth.generator import generate_to, params_from
from tests.backend.test_synth_generation import CFG

ROOT = Path(__file__).resolve().parents[2]
RUNNER = """
import json, sys, traceback
from pathlib import Path
import tests.backend.test_synth_generation as gen
gen.REFERENCE = Path(sys.argv[1])
gen.read.__defaults__ = (gen.REFERENCE,)
import tests.backend.test_synth_scenarios as sc
observable = sc.test_scenario_is_observable_in_the_generated_data
checks = [(sid, lambda sid=sid: observable(sid)) for sid in sorted(sc.SC)]
skip = (
    "test_scenario_is_observable_in_the_generated_data",
    "test_every_ground_truth_scenario_has_a_data_check",
)
checks += [(n, getattr(sc, n)) for n in dir(sc) if n.startswith("test_") and n not in skip]
failures = []
for name, fn in checks:
    try:
        fn()
    except Exception as e:
        frame = traceback.extract_tb(e.__traceback__)[-1]
        failures.append(f"{name}: line {frame.lineno}: {frame.line}")
print(json.dumps({"scenarios": len(sc.SC), "checks": len(checks), "failures": failures}))
"""


@pytest.mark.parametrize(
    "overrides",
    [
        {"seed": 43},
        {"seed": 7, "scale": 0.3, "years": 2},
        {"seed": 2024, "start": date(2021, 1, 1), "years": 4},
    ],
    ids=["seed43", "seed7-small-2y", "seed2024-2021-4y"],
)
def test_scenarios_hold_for_other_variants(overrides, tmp_path):
    generate_to(params_from(CFG, **overrides), tmp_path)
    done = subprocess.run(
        [sys.executable, "-c", RUNNER, str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert done.returncode == 0, done.stderr[-2000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["scenarios"] == 63 and result["checks"] > 63
    assert result["failures"] == []
