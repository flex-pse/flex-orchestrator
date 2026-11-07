"""Run a scenario repeatedly in fresh processes and summarise how each ended."""

import json
import os
import subprocess
import sys
import time

scenario, iterations, version = sys.argv[1], int(sys.argv[2]), sys.argv[3]
BUDGET_S = 40 * 60
DRIVER = [sys.executable, ".github/ray_repro/driver.py", scenario]
PYTEST = [sys.executable, "-m", "pytest", "src/flex_orchestrator/study/tests/test_parallel.py",
          "-m", "component", "-q", "-p", "no:cacheprovider", "-W", "ignore", "--tb=line"]
command = PYTEST if scenario == "pytest" else DRIVER
timeout = 900 if scenario == "pytest" else 240
os.makedirs("repro_logs", exist_ok=True)
runs, started = [], time.monotonic()
for i in range(iterations):
    if time.monotonic() - started > BUDGET_S:
        break
    t0 = time.monotonic()
    try:
        p = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=timeout)
        code, out = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as exc:
        code, out = "timeout", str(exc.stdout or "") + str(exc.stderr or "")
    fatal = "Windows fatal exception" in out or "Fatal Python error" in out
    in_shutdown = fatal and "in shutdown" in out
    runs.append({"i": i, "code": code, "fatal": fatal, "in_shutdown": in_shutdown,
                 "seconds": round(time.monotonic() - t0, 1)})
    with open(f"repro_logs/{scenario}_{version}_{i}.log", "w", encoding="utf-8") as f:
        f.write(out)
    print(runs[-1], flush=True)
summary = {"scenario": scenario, "ray": version, "platform": sys.platform, "runs": len(runs),
           "fatal": sum(r["fatal"] for r in runs), "in_shutdown": sum(r["in_shutdown"] for r in runs),
           "nonzero": sum(r["code"] != 0 for r in runs), "codes": sorted({str(r["code"]) for r in runs}),
           "mean_s": round(sum(r["seconds"] for r in runs) / max(len(runs), 1), 1), "detail": runs}
with open(f"repro_logs/summary_{scenario}_{version}.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, indent=1)
print({k: v for k, v in summary.items() if k != "detail"})
