r"""A tenant's smoke study, run inside its stack (PLAN.md M6).

``Orchestrator()`` takes no arguments: ``FLEXORCH_ACQUIRIUM_URL`` points it at
the tenant's Acquirium (mode 3). The study stores operating data and the
flex-pse demo spec, then solves the demo twice with its pump flow set from the
data (``model_inputs``), today's and yesterday's.

    docker compose -p flexorch-<tenant> -f compose.tenant.yaml run --rm \
        -v "$PWD/smoke.py:/scripts/smoke.py:ro" orchestrator \
        python /scripts/smoke.py [study]

Run again, it skips what's stored. It prints the solved costs as JSON.
"""

import json
import sys

import numpy as np
import pandas as pd

from flex_orchestrator import Orchestrator
from flex_orchestrator.errors import TagNotFoundError
from flex_orchestrator.testing import DEMO_SPEC

FLOW = "demo.pump.flow_in"
PUMP = {"dataset": "pump", "columns": ["flow"], "rename": {"flow": FLOW}}

if __name__ == "__main__":
    study = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    with Orchestrator() as orch:
        assert orch.connection.mode == "remote", orch.connection
        orch.get_or_create_study(study)
        try:
            orch.get_data("pump")
        except TagNotFoundError:
            hours = pd.date_range("2025-07-07", periods=48, freq="h", tz="UTC")
            flow = 3.0 + np.arange(48) / 8  # rising, so yesterday ran lower
            orch.store_data(
                "pump", pd.DataFrame({"flow": flow}, hours), units={"flow": "m^3/hr"}
            )
        if "demo" not in [spec.tag for spec in orch.stored_specs()]:
            orch.store_model_spec("demo", DEMO_SPEC)
        run = orch.execute_study(
            {
                "base_model_spec": "demo",
                "sweeps": [
                    {
                        "type": "model_inputs",
                        "name": "day",
                        "values": [
                            {"label": "today", "inputs": [PUMP]},
                            {
                                "label": "yesterday",
                                "inputs": [{**PUMP, "offset": "-1D"}],
                            },
                        ],
                    }
                ],
                "export": {"include": [FLOW, "costing.aggregate_operating_cost"]},
                "output_name": "{day}",
                "on_existing": "skip",
            }
        )
        costs = {
            r.tag: r.scalars()["costing.aggregate_operating_cost"]
            for r in orch.stored_model_results()
        }
        print(json.dumps({"study": study, "counts": run.counts(), "costs": costs}))
