"""``flex-orch``: run studies and commands, export and import (PLAN.md M3, M4).

::

    flex-orch [CONNECTION] run study.yaml [--study S] [--workers N]
                                          [--ray-address A] [--dry-run]
    flex-orch [CONNECTION] cmd NAME [--study S] [--OPTION VALUE ...]
    flex-orch [CONNECTION] commands
    flex-orch [CONNECTION] export STUDY PATH [--parts ...] [--tags ...]
                                          [--compress] [--overwrite]
    flex-orch [CONNECTION] import PATH [--name N] [--on-existing error|skip]
    flex-orch [CONNECTION] verify [--study S ...] [--samples N] [--against FILE]
    flex-orch [CONNECTION] serve [--local] [--host H] [--port P]  (needs [server])
    flex-orch login [--url URL]                  (the token comes on stdin)
    flex-orch [--data-dir D] acquirium start [--config TOML] [--port P]
    flex-orch [--data-dir D] acquirium status | stop
    flex-orch openapi [--output FILE]            (needs [server])

``CONNECTION`` picks the store as ``Orchestrator()`` does (D11):
``--data-dir`` (local, the default ``.flexorch``), ``--db`` with
``--data-dir`` (Postgres; the login comes from ``FLEXORCH_DB_*`` or libpq,
never the command line), or ``--acquirium-url``. ``--files DIR`` uses a
folder on disk instead, with no Acquirium
(:class:`~flex_orchestrator.backend.FileBackend`). ``--plugin NAME`` loads an
installed plugin on top of those in ``FLEXORCH_PLUGINS``.

``cmd`` builds its options from the command's params model: ``--field-name``
per field, numbers and strings as typed, lists as several values, booleans as
``--flag`` / ``--no-flag``, and anything else as JSON. Every subcommand prints
its result as JSON. ``verify`` exits 1 when the store has a problem or, with
``--against``, differs from that earlier report.

``serve`` runs the tenant's HTTP API (Phase 2) on that store, configured by
the ``FLEXORCH_*`` environment (:class:`~flex_orchestrator.server.Settings`).
With ``--local`` it takes no tokens: every request is you, as owner, and it
serves this machine only (loopback hosts).
``acquirium start`` runs a long-lived Acquirium on the data dir (or your own
``acquirium.toml`` with its drivers), which outlives every script; while it
runs, everything that opens that data dir attaches to it
(:mod:`flex_orchestrator.service`). ``status`` and ``stop`` find it there.
``login`` saves a token for ``Orchestrator(url=...)``: read from stdin, or
asked for without echo, never from the command line; with ``--url`` it is
checked first. ``openapi`` prints (or writes) the core routes' OpenAPI schema,
which a UI's client is generated from.
"""

import argparse
import dataclasses
import json
import sys
import types
import typing
from collections.abc import Sequence
from typing import Any

from flex_orchestrator.errors import FlexOrchestratorError
from flex_orchestrator.ext.commands import to_json


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``flex-orch`` with ``argv``; return the exit code."""
    parser = _parser()
    args, rest = parser.parse_known_args(argv)
    if args.action != "cmd" and rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    if args.files and args.data_dir:
        parser.error("--files is the store itself; it takes no --data-dir")
    if args.action == "login":
        return _login(args)
    if args.action == "serve":
        return _serve(args)
    if args.action == "acquirium":
        return _acquirium(args)
    if args.action == "openapi":
        return _openapi(args)
    try:
        with _orchestrator(args) as orch:
            result = _run(orch, args, rest)
    except FlexOrchestratorError as exc:
        print(f"flex-orch: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(to_json(result), indent=2))
    return 0 if args.action != "verify" or result["ok"] else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flex-orch",
        description="Run studies and commands, and export and import studies.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--acquirium-url", help="a running Acquirium (mode 3)")
    mode.add_argument(
        "--db", help="Postgres URL without a password, with --data-dir (mode 2)"
    )
    mode.add_argument("--files", metavar="DIR", help="a folder on disk, no Acquirium")
    parser.add_argument(
        "--data-dir", help="the managed Acquirium's folder (default .flexorch)"
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        metavar="NAME",
        help="load this installed plugin too (repeatable)",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    run = actions.add_parser("run", help="run a study config")
    run.add_argument("config", help="the study config, YAML or JSON")
    run.add_argument("--study", help="the study (if the config doesn't name one)")
    run.add_argument("--workers", type=int, help="tasks at once (>1 runs on Ray)")
    run.add_argument(
        "--ray-address",
        help="a Ray cluster, e.g. ray://head:10001 (local Ray if unset)",
    )
    run.add_argument(
        "--dry-run", action="store_true", help="print the plan, run nothing"
    )

    cmd = actions.add_parser(
        "cmd", help="run a command; its options come from its parameters"
    )
    cmd.add_argument("name", help="the command")
    cmd.add_argument("--study", help="the study to select first")

    actions.add_parser("commands", help="list the commands")

    export = actions.add_parser("export", help="export a study to a folder")
    export.add_argument("study")
    export.add_argument("path")
    export.add_argument("--parts", nargs="+", help="specs, data, fits, results")
    export.add_argument("--tags", nargs="+", help="only these tags")
    export.add_argument("--compress", action="store_true", help="write .json.gz")
    export.add_argument("--overwrite", action="store_true", help="replace PATH")

    imported = actions.add_parser("import", help="import an exported study")
    imported.add_argument("path")
    imported.add_argument("--name", help="the new study's name")
    imported.add_argument("--on-existing", choices=["error", "skip"], default="error")

    check = actions.add_parser(
        "verify", help="check the store and fingerprint it (after a restore, say)"
    )
    check.add_argument("--study", nargs="+", help="only these studies")
    check.add_argument(
        "--samples", type=int, default=5, help="items per kind to read in full"
    )
    check.add_argument(
        "--against", metavar="FILE", help="an earlier report to compare with"
    )

    serve = actions.add_parser("serve", help="run the tenant's HTTP API")
    serve.add_argument(
        "--local", action="store_true", help="no tokens: you, on this machine only"
    )
    serve.add_argument("--host", default="127.0.0.1", help="bind host")
    serve.add_argument("--port", type=int, default=8080, help="bind port")

    service = actions.add_parser(
        "acquirium", help="a long-lived local Acquirium: start, status or stop"
    )
    service.add_argument("service_action", choices=["start", "status", "stop"])
    service.add_argument("--config", help="your acquirium.toml (start only)")
    service.add_argument("--port", type=int, help="its port (start; free if unset)")

    openapi = actions.add_parser("openapi", help="the core routes' OpenAPI schema")
    openapi.add_argument("--output", help="write it here instead of printing it")

    login = actions.add_parser(
        "login", help="save a token for Orchestrator(url=...); it comes on stdin"
    )
    login.add_argument("--url", help="check the token against this tenant first")
    return parser


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from flex_orchestrator.server import Settings, create_app
    except ImportError as exc:
        print(
            f"flex-orch: serve needs flex-orchestrator[server] ({exc})", file=sys.stderr
        )
        return 1
    if args.local and args.host not in ("127.0.0.1", "::1", "localhost"):
        print(
            "flex-orch: --local serves loopback only (--host 127.0.0.1)",
            file=sys.stderr,
        )
        return 1
    try:
        settings = Settings.local() if args.local else Settings.from_env()
    except ValueError as exc:
        print(f"flex-orch: {exc}", file=sys.stderr)
        return 1
    store = {
        key: value
        for key, value in (
            ("data_dir", args.data_dir),
            ("db", args.db),
            ("acquirium_url", args.acquirium_url),
        )
        if value is not None
    }
    if args.files:
        from flex_orchestrator.backend.files import FileBackend

        store = {"backend": FileBackend(args.files)}
    settings = dataclasses.replace(
        settings,
        plugins=tuple(args.plugin),
        orchestrator={**settings.orchestrator, **store},
    )
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        root_path=settings.root_path,  # FLEXORCH_ROOT_PATH, behind a gateway
    )
    return 0


def _openapi(args: argparse.Namespace) -> int:
    try:
        from flex_orchestrator.server.openapi import core_schema, dumps
    except ImportError as exc:
        print(
            f"flex-orch: openapi needs flex-orchestrator[server] ({exc})",
            file=sys.stderr,
        )
        return 1
    text = dumps(core_schema())
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
    else:
        sys.stdout.write(text)
    return 0


def _acquirium(args: argparse.Namespace) -> int:
    import dataclasses as dc

    from flex_orchestrator import service

    if args.db or args.acquirium_url or args.files:
        print(
            "flex-orch: acquirium takes --data-dir (or --config) only", file=sys.stderr
        )
        return 1
    try:
        if args.service_action == "start":
            found = dc.asdict(
                service.start(args.data_dir, config=args.config, port=args.port)
            )
        elif args.service_action == "stop":
            stopped = service.stop(args.data_dir)
            found = {"stopped": stopped is not None}
        else:
            found = service.status(args.data_dir)
    except (FlexOrchestratorError, ValueError) as exc:
        print(f"flex-orch: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(found, indent=2))
    return 0


def _login(args: argparse.Namespace) -> int:
    import getpass

    from flex_orchestrator.client import RemoteOrchestrator, save_token

    if sys.stdin.isatty():
        token = getpass.getpass("Token: ")
    else:
        token = sys.stdin.readline()
    token = token.strip()
    if not token:
        print("flex-orch: no token given", file=sys.stderr)
        return 1
    if args.url:
        try:
            RemoteOrchestrator(args.url, token).studies()
        except FlexOrchestratorError as exc:
            print(f"flex-orch: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    print(json.dumps({"saved": str(save_token(token))}))
    return 0


def _orchestrator(args: argparse.Namespace) -> Any:
    from flex_orchestrator.ext import create_orchestrator

    if args.files:
        from flex_orchestrator.backend.files import FileBackend

        return create_orchestrator(FileBackend(args.files), plugins=args.plugin)
    return create_orchestrator(
        data_dir=args.data_dir,
        db=args.db,
        acquirium_url=args.acquirium_url,
        plugins=args.plugin,
    )


def _run(orch: Any, args: argparse.Namespace, rest: list[str]) -> Any:
    if args.action == "run":
        from flex_orchestrator.study.config import load_config

        config = load_config(args.config)
        if config.study is None:
            if args.study is not None:
                orch.get_or_create_study(args.study)
            elif orch._study is None:
                raise FlexOrchestratorError(
                    "Name the study: in the config (study:) or with --study."
                )
        return orch.execute_study(
            config,
            max_workers=args.workers,
            ray_address=args.ray_address,
            dry_run=args.dry_run,
        )
    if args.action == "commands":
        return [
            {
                "name": c.name,
                "description": c.description,
                "source": orch.commands.source(c.name),
            }
            for c in orch.commands
        ]
    if args.action == "export":
        orch.select_study(args.study)
        report = orch.export_study(
            args.path,
            parts=args.parts or ("specs", "data", "fits", "results"),
            tags=args.tags,
            compress=args.compress,
            overwrite=args.overwrite,
        )
        return {"path": str(report.path), "counts": report.counts}
    if args.action == "import":
        report = orch.import_study(
            args.path, name=args.name, on_existing=args.on_existing
        )
        return {
            "study": report.study,
            "counts": report.counts,
            "skipped": report.skipped,
        }
    if args.action == "verify":
        from flex_orchestrator.verify import compare, verify

        report = verify(orch, args.study, args.samples)
        if args.against is not None:
            with open(args.against, encoding="utf-8") as file:
                report["differences"] = compare(json.load(file), report)
        report["ok"] = not report["problems"] and not report.get("differences")
        return report
    cmd = orch.commands.get(args.name)
    if args.study is not None:
        orch.select_study(args.study)
    params = command_parser(cmd).parse_args(rest)
    return orch.run_command(cmd.name, dict(vars(params)))


def command_parser(cmd: Any) -> argparse.ArgumentParser:
    """Return the argument parser for a command's params model."""
    parser = argparse.ArgumentParser(
        prog=f"flex-orch cmd {cmd.name}", description=cmd.description
    )
    for name, info in cmd.params_model.model_fields.items():
        flag = "--" + name.replace("_", "-")
        kwargs: dict[str, Any] = {"dest": name, "help": info.description}
        annotation = _unwrap(info.annotation)
        scalar = _scalar(annotation)
        item = _list_item(annotation)
        if annotation is bool:
            kwargs["action"] = argparse.BooleanOptionalAction
        elif scalar is not None:
            kwargs["type"] = scalar
        elif item is not None:
            kwargs.update(nargs="+", type=item)
        else:
            kwargs.update(type=_json_value, metavar="JSON")
        if info.is_required():
            kwargs["required"] = True
        else:
            kwargs["default"] = argparse.SUPPRESS
        parser.add_argument(flag, **kwargs)
    return parser


def _unwrap(annotation: Any) -> Any:
    """Drop ``None`` from an optional annotation."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _scalar(annotation: Any) -> Any:
    return annotation if annotation in (int, float, str) else None


def _list_item(annotation: Any) -> Any:
    if typing.get_origin(annotation) in (list, Sequence, tuple):
        args = typing.get_args(annotation)
        if len(args) >= 1 and _scalar(args[0]) is not None:
            return args[0]
    return None


def _json_value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not JSON: {exc}") from exc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
