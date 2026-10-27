"""Run a study on a flex-orchestrator server in local mode, over its HTTP API.

Starts a local server on a fresh store, then drives it as a client. Local
mode takes no tokens and serves this machine only; from a shell it's

    flex-orch --data-dir examples/server_examples/tmp_flexorch serve --local

``Orchestrator(url=...)`` has the local orchestrator's methods, so
``new_study`` and the ``sweep_surrogates`` config are used unchanged; each
call is one request, which the server logs. Its routes are listed at
``<url>/docs`` while it runs.

    python examples/server_examples/local_mode.py

With users and tokens, see ``users_and_tokens.py``.
"""

from serving import HERE, STUDY, new_study, report, start_server

from flex_orchestrator import Orchestrator

if __name__ == "__main__":
    server, thread, url = start_server()
    try:
        orch = Orchestrator(url=url)  # local mode: no token
        # POST /studies, /studies/{s}/data/{name} and /specs/model/{tag}
        # (the spec's tariff file goes with it)
        new_study("ro_local_mode", orch)
        print("studies:", orch.studies())

        # POST /studies/{s}/execute queues the study as a job; GET /jobs/{id}
        # follows it. execute_study(STUDY) on its own waits too.
        job = orch.execute_study(STUDY, wait=False)
        orch.wait(job["job_id"])
        print(f"job {job['job_id']}: {orch.job(job['job_id'])['status']}")

        # GET /studies/{s}/results and /results/{tag}
        report(orch)
        series = orch.result("linear_surrogate").to_frame("pandas")
        print(f"linear_surrogate stores {len(series.columns)} series")

        # GET /commands, POST /commands/{name}
        print("commands:", [c["name"] for c in orch.commands()])

        # GET /studies/{s}/export: the study as a zip, as export_study writes it
        folder = orch.export_study(HERE / "tmp_flexorch" / "local_mode_export")
        print("exported to", folder)
    finally:
        server.should_exit = True  # the app closes its store as it stops
        thread.join()
