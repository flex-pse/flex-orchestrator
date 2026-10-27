"""The client for a deployed tenant (PLAN.md D15, M7).

::

    from flex_orchestrator import Orchestrator

    orch = Orchestrator(url="https://flex.example.org/tenant-a", token=...)
    orch.select_study("example_a")
    orch.store_data("ops", frame, units={"q": "m^3/hr"})
    run = orch.execute_study(config)              # waits for the job
    for result in orch.stored_model_results():
        result.to_frame("pandas")

``Orchestrator(url=...)`` returns a :class:`RemoteOrchestrator`: the methods
the tenant's HTTP routes back, under the same names. Building a model stays on
your side: fetch the spec (:meth:`RemoteOrchestrator.model_spec`) and build it.
The token is ``token=``, else ``FLEXORCH_TOKEN``, else the one ``flex-orch
login`` saved; a local server (``flex-orch serve --local``) needs none.
Errors come back as the library's own (``TagNotFoundError``,
``TagExistsError``, ...); a refused token is
:class:`~flex_orchestrator.errors.AccessError`.

It uses only the standard library's HTTP client.
"""

import base64
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from flex_orchestrator import errors

TOKEN_FILE = Path.home() / ".flexorch" / "token"
"""Path: Where ``flex-orch login`` saves the token."""

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]
"""``(method, url, headers, body) -> (status, body)``: how requests are sent."""


@dataclass
class RemoteSpec:
    """A stored spec, as the server returns it.

    Attributes:
        tag: Its tag.
        kind: ``model_spec`` or ``surrogate_spec``.
        spec: The spec, as JSON.
        spec_hash: Its hash.
        spec_format: Its format.
        parent_tag: The spec it was derived from.
        metadata: Its run metadata.
    """

    tag: str
    kind: str
    spec: dict[str, Any]
    spec_hash: str
    spec_format: str
    parent_tag: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def read_token(environ: Mapping[str, str] | None = None) -> str | None:
    """Return ``FLEXORCH_TOKEN``, else the token ``flex-orch login`` saved."""
    env = os.environ if environ is None else environ
    if env.get("FLEXORCH_TOKEN"):
        return env["FLEXORCH_TOKEN"]
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def save_token(token: str, path: Path | None = None) -> Path:
    """Save ``token`` for later calls, readable only by you (``flex-orch login``).

    Args:
        token: The token.
        path: Where; :data:`TOKEN_FILE` by default.
    """
    path = TOKEN_FILE if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(token.strip() + "\n")
    os.chmod(path, 0o600)
    return path


class RemoteOrchestrator:
    """A tenant's orchestrator, over its HTTP API.

    Args:
        url: The tenant's base URL, e.g. ``https://flex.example.org/tenant-a``.
        token: The bearer token; :func:`read_token` if ``None``. A local
            server needs none.
        study: A study to select.
        frame: ``polars`` or ``pandas``, for :meth:`get_data`.
        timeout: Seconds per request.
        transport: How requests are sent (tests pass an in-process one).
        environ: Where ``FLEXORCH_TOKEN`` is read.
    """

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        study: str | None = None,
        frame: str = "polars",
        timeout: float = 60.0,
        transport: Transport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Connect to ``url``; see the class docstring."""
        self.url = url.rstrip("/")
        self._token = token or read_token(environ)
        self.frame = frame
        self.timeout = timeout
        self._transport = transport or self._urllib
        self._study: str | None = None
        if study is not None:
            self.select_study(study)

    def __repr__(self) -> str:
        """Show where it points, never the token."""
        return f"RemoteOrchestrator({self.url!r}, study={self._study!r})"

    def __enter__(self) -> "RemoteOrchestrator":
        """Use it in a ``with`` block, like :class:`Orchestrator`."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Nothing to close: each call is its own request."""

    def close(self) -> None:
        """Nothing to close: each call is its own request."""

    # Studies

    @property
    def study(self) -> str:
        """The selected study.

        Raises:
            StudyNotFoundError: If none is selected.
        """
        if self._study is None:
            raise errors.StudyNotFoundError(
                "No study is selected; call create_study() or select_study() first."
            )
        return self._study

    def studies(self) -> list[str]:
        """Return the tenant's studies."""
        return self._json("GET", "/studies")["studies"]

    def create_study(self, name: str) -> str:
        """Create a study and select it (``StudyExistsError`` if it exists)."""
        self._study = self._json("POST", "/studies", {"name": name})["study"]
        return self._study

    def select_study(self, name: str) -> str:
        """Select an existing study (``StudyNotFoundError`` if it doesn't)."""
        self._json("GET", f"/studies/{_q(name)}")
        self._study = name
        return name

    def get_or_create_study(self, name: str) -> str:
        """Select the study, creating it if it doesn't exist."""
        if name in self.studies():
            return self.select_study(name)
        return self.create_study(name)

    def delete_study(self, name: str) -> None:
        """Delete a study and everything in it, for good (owner role)."""
        self._json("DELETE", f"/studies/{_q(name)}")
        if self._study == name:
            self._study = None

    def delete(self, kind: str, tag: str) -> None:
        """Delete a stored item, for good (owner role)."""
        self._json("DELETE", f"{self._s()}/{_q(kind)}/{_q(tag)}")

    # Specs

    def store_model_spec(
        self,
        tag: str,
        spec: Any,
        *,
        parent: str | None = None,
        base_dir: str | os.PathLike[str] | None = None,
        assets: Iterable[str] = (),
        spec_format: str | None = None,
        replace: bool = False,
    ) -> RemoteSpec:
        """Store a model spec, with the files it references, as the library does.

        The files (a flex-pse tariff, events or ``$source`` file, or
        ``assets`` pointers) are read here, relative to ``base_dir``, and sent
        with the spec. Names the ``parent`` stored are left for the server.
        """
        from flex_orchestrator.assets import (
            ASSET_DIR,
            collect_assets,
            find_assets,
            set_pointer,
        )
        from flex_orchestrator.specs import detect_format, to_json_spec

        data = to_json_spec(spec)
        spec_format = spec_format or detect_format(data)
        references = {
            pointer: path
            for pointer, path in find_assets(data, spec_format, assets).items()
            if parent is None or not path.startswith(f"{ASSET_DIR}/")
        }
        data, refs, read = collect_assets(data, references, base_dir)
        # Each file goes as <sha256[:12]>/<filename>, which the server stores
        # as assets/<sha256[:12]>-<filename>, the name it has here.
        files = {}
        for pointer, ref in refs.items():
            digest, _, filename = ref.name.removeprefix(f"{ASSET_DIR}/").partition("-")
            set_pointer(data, pointer, f"{digest}/{filename}")
            files[f"{digest}/{filename}"] = base64.b64encode(read[ref.name]).decode()
        body = {
            "spec": data,
            "parent": parent,
            "spec_format": spec_format,
            "assets": list(assets),
            "files": files,
            "replace": replace,
        }
        return RemoteSpec(
            **self._json("POST", f"{self._s()}/specs/model/{_q(tag)}", body)
        )

    def store_surrogate_spec(
        self, tag: str, spec: Mapping[str, Any], *, replace: bool = False
    ) -> RemoteSpec:
        """Store a surrogate spec."""
        body = {"spec": dict(spec), "replace": replace}
        return RemoteSpec(
            **self._json("POST", f"{self._s()}/specs/surrogate/{_q(tag)}", body)
        )

    def model_spec(self, tag: str) -> RemoteSpec:
        """Return a stored model spec."""
        return RemoteSpec(**self._json("GET", f"{self._s()}/specs/model/{_q(tag)}"))

    def surrogate_spec(self, tag: str) -> RemoteSpec:
        """Return a stored surrogate spec."""
        return RemoteSpec(**self._json("GET", f"{self._s()}/specs/surrogate/{_q(tag)}"))

    def stored_specs(self, kind: str = "model_spec") -> list[RemoteSpec]:
        """Return the study's specs of one kind, oldest first."""
        path = "model" if kind == "model_spec" else "surrogate"
        tags = self._json("GET", f"{self._s()}/specs/{path}")["tags"]
        read = self.model_spec if path == "model" else self.surrogate_spec
        return [read(tag) for tag in tags]

    # Datasets

    def store_data(
        self,
        name: str,
        data: Any,
        *,
        units: Mapping[str, str] | None = None,
        time_column: str | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        """Store a dataset: a pandas frame on a time index, or with ``time_column``.

        Returns:
            ``{"name", "run_id", "rows"}``.
        """
        frame = data.to_pandas() if hasattr(data, "to_pandas") else data
        if time_column is not None or "time" in frame.columns:
            frame = frame.set_index(time_column or "time")
        index = pd.DatetimeIndex(frame.index)
        body = {
            "time_index": [t.isoformat() for t in index],
            "columns": {
                str(c): {
                    "unit": (units or {}).get(str(c)),
                    "values": [None if pd.isna(v) else float(v) for v in frame[c]],
                }
                for c in frame.columns
            },
            "replace": replace,
        }
        return self._json("POST", f"{self._s()}/data/{_q(name)}", body)

    def get_data(
        self, name: str, columns: Iterable[str] | None = None, frame: str | None = None
    ) -> Any:
        """Return a dataset, as ``frame`` (``polars`` or ``pandas``)."""
        query = "".join(f"&columns={_q(c)}" for c in columns or ()).lstrip("&")
        found = self._json(
            "GET", f"{self._s()}/data/{_q(name)}" + (f"?{query}" if query else "")
        )
        table = pd.DataFrame(
            {c: v["values"] for c, v in found["columns"].items()},
            index=pd.DatetimeIndex(pd.to_datetime(found["time_index"], utc=True)),
        )
        table.index.name = "time"
        if (frame or self.frame) == "pandas":
            return table
        import polars as pl

        return pl.from_pandas(table.reset_index())

    # Results

    def stored_model_results(self, tags: Iterable[str] | None = None) -> list[Any]:
        """Return the study's results, oldest first, read in pages."""
        from flex_orchestrator.archive.load import LoadedResult
        from flex_orchestrator.archive.models import ResultFile

        query = "".join(f"&tags={_q(t)}" for t in tags or ())
        found, offset = [], 0
        while offset is not None:
            page = self._json(
                "GET", f"{self._s()}/results?limit=200&offset={offset}{query}"
            )
            found += [
                LoadedResult(ResultFile.model_validate(f))
                for f in page["results"].values()
            ]
            offset = page["next"]
        return found

    def result(self, tag: str) -> Any:
        """Return one result (works like a ``ResultHandle``, offline)."""
        from flex_orchestrator.archive.load import LoadedResult
        from flex_orchestrator.archive.models import ResultFile

        return LoadedResult(
            ResultFile.model_validate(
                self._json("GET", f"{self._s()}/results/{_q(tag)}")
            )
        )

    # Commands and jobs

    def commands(self) -> list[dict[str, Any]]:
        """Return every command the tenant has, with its parameters' schema."""
        return self._json("GET", "/commands")["commands"]

    def run_command(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        /,
        *,
        wait: bool = True,
        timeout: float | None = None,
        **fields: Any,
    ) -> Any:
        """Run a command in the selected study (if any).

        A long-running one is a job: with ``wait`` this returns its result (or
        raises its error); without, ``{"job_id", ...}``.
        """
        body = {**dict(params or {}), **fields}
        query = f"?study={_q(self._study)}" if self._study else ""
        status, found = self._request("POST", f"/commands/{_q(name)}{query}", body)
        if status == 202:
            return self.wait(found["job_id"], timeout) if wait else found
        return found["result"]

    def execute_study(
        self, config: Any, *, wait: bool = True, timeout: float | None = None
    ) -> Any:
        """Run a study config in the selected study, on the tenant's job queue.

        Returns:
            With ``wait``, the :class:`~flex_orchestrator.study.StudyRun`;
            without, ``{"job_id", ...}``.
        """
        from flex_orchestrator.study.runner import StudyRun

        if hasattr(config, "model_dump"):
            config = config.model_dump(mode="json", exclude_defaults=True)
        _, found = self._request("POST", f"{self._s()}/execute", dict(config))
        if not wait:
            return found
        return StudyRun.model_validate(self.wait(found["job_id"], timeout))

    def fit_surrogate(
        self, *, wait: bool = True, timeout: float | None = None, **params: Any
    ) -> Any:
        """Fit surrogates (D19) in the selected study, as ``fit_surrogate`` does."""
        _, found = self._request("POST", f"{self._s()}/surrogates/fit", params)
        return self.wait(found["job_id"], timeout) if wait else found

    def job(self, job_id: str) -> dict[str, Any]:
        """Return a job's status, progress, result or error."""
        return self._json("GET", f"/jobs/{_q(job_id)}")

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a queued or running job."""
        return self._json("DELETE", f"/jobs/{_q(job_id)}")

    def wait(self, job_id: str, timeout: float | None = None) -> Any:
        """Wait for a job; return its result.

        Raises:
            CommandError: If it failed, was cancelled or interrupted.
            TimeoutError: If it's still going after ``timeout`` seconds.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        pause = 0.2
        while True:
            found = self.job(job_id)
            if found["status"] == "succeeded":
                return found["result"]
            if found["status"] not in ("queued", "running"):
                raise errors.CommandError(
                    f"Job {job_id} ({found['command']}) {found['status']}: "
                    f"{found.get('error')}"
                )
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"Job {job_id} is still {found['status']}.")
            time.sleep(pause)
            pause = min(pause * 1.5, 5.0)

    # Export and import (D17)

    def export_study(self, path: str | os.PathLike[str]) -> Path:
        """Download the selected study's export into ``path`` (new or empty)."""
        target = Path(path)
        if target.exists() and any(target.iterdir()):
            raise errors.ArchiveError(f"{target} isn't an empty folder.")
        _, data = self._send("GET", f"{self._s()}/export", None, {})
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(target)
        return target

    def import_study(
        self,
        path: str | os.PathLike[str],
        *,
        name: str | None = None,
        on_existing: str = "error",
        wait: bool = True,
        timeout: float | None = None,
    ) -> Any:
        """Upload an export folder as a new study (or into one, ``skip``)."""
        folder = Path(path)
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(folder.rglob("*")):
                if file.is_file():
                    archive.write(file, file.relative_to(folder).as_posix())
        fields = {"on_existing": on_existing, **({"name": name} if name else {})}
        body, content_type = _multipart(fields, "file", "export.zip", packed.getvalue())
        status, data = self._send(
            "POST", "/studies/import", body, {"Content-Type": content_type}
        )
        found = self._decode(status, data)
        return self.wait(found["job_id"], timeout) if wait else found

    # Plumbing

    def _s(self) -> str:
        return f"/studies/{_q(self.study)}"

    def _json(self, method: str, path: str, body: Any = None) -> Any:
        return self._request(method, path, body)[1]

    def _request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        payload = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if payload else {}
        status, data = self._send(method, path, payload, headers)
        return status, self._decode(status, data)

    def _send(
        self, method: str, path: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        if self._token:
            headers = {**headers, "Authorization": f"Bearer {self._token}"}
        status, data = self._transport(method, self.url + path, headers, body)
        if status >= 400:
            self._decode(status, data)
        return status, data

    def _decode(self, status: int, data: bytes) -> Any:
        try:
            found = json.loads(data) if data else None
        except ValueError:
            found = None
        if status < 400:
            return found
        detail = (found or {}).get("detail", data[:200].decode(errors="replace"))
        if status in (401, 403):
            if status == 401 and not self._token:
                detail = (
                    "No token: pass token=, set FLEXORCH_TOKEN or run flex-orch login."
                )
            raise errors.AccessError(f"{status}: {detail}", status)
        name = (found or {}).get("error")
        cls = getattr(errors, name, None) if isinstance(name, str) else None
        if isinstance(cls, type) and issubclass(cls, errors.FlexOrchestratorError):
            raise cls(detail)
        if status == 404:
            raise errors.TagNotFoundError(str(detail))
        if status == 422:
            raise errors.CommandError(json.dumps(detail))
        raise errors.StorageError(f"The server answered {status}: {detail}")

    def _urllib(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout
            ) as response:  # noqa: S310
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise errors.StorageError(f"Can't reach {self.url}: {exc.reason}") from None


def _q(text: str) -> str:
    return urllib.parse.quote(str(text), safe="")


def _multipart(
    fields: Mapping[str, str], name: str, filename: str, data: bytes
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
        f'filename="{filename}"\r\nContent-Type: application/zip\r\n\r\n'.encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
