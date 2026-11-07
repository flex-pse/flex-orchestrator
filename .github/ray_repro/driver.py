"""One Ray session, then ray.shutdown(); no flex-orchestrator involved."""

import faulthandler
import os
import sys
import tempfile
import time

import ray

faulthandler.enable()


@ray.remote
def work(x, blob):
    return x + len(blob)


@ray.remote
def sleeper(seconds):
    time.sleep(seconds)


@ray.remote(max_retries=0)
def die():
    os._exit(1)


@ray.remote(max_calls=1)
def once(x):
    return os.getpid()


def tasks():
    blob = ray.put(b"x" * 100_000)
    refs = [work.remote(i, blob) for i in range(50)]
    pending = list(refs)
    while pending:
        ready, pending = ray.wait(pending, num_returns=1, timeout=1)
        ray.get(ready)
    return refs, blob  # still held at shutdown


def hostile():
    kept = list(tasks())
    kept.append(ray.get([once.remote(i) for i in range(8)]))
    hung = [sleeper.remote(600) for _ in range(3)]
    time.sleep(2)
    for ref in hung:
        ray.cancel(ref, force=True)
    dead = die.remote()
    try:
        ray.get(dead)
    except Exception:
        pass
    kept += [hung, dead, sleeper.remote(600)]  # one still running at shutdown
    return kept


scenario = sys.argv[1]
ray.init(address="local", include_dashboard=False, num_cpus=4, log_to_driver=False,
         _temp_dir=tempfile.mkdtemp(prefix="r", dir="C:/") if os.name == "nt" else None)
kept = {"bare": lambda: None, "tasks": tasks, "hostile": hostile}[scenario]()
print("shutting down", flush=True)
ray.shutdown()
print("clean", flush=True)
