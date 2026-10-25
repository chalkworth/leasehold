"""A policy simulator for leasehold: simulated agents and jobs on one machine, admitted by the
library's own admission code, to compare sizing rules and range defaults before the real
benchmark (`bench/run.py`) decides. `python -m bench.sim --help`; docs/sim.md has the method,
the calibration and its limits.
"""

import sys
from pathlib import Path

# `bench/run.py` imports its sibling modules by their bare names, as a script does.
_BENCH = str(Path(__file__).resolve().parent.parent)
if _BENCH not in sys.path:
    sys.path.append(_BENCH)
