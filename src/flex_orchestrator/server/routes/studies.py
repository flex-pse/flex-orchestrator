"""Studies, specs, datasets, results and deletion (PLAN.md §4).

Results and datasets travel in the D17 export's JSON (``ResultFile``,
``DatasetFile``, the same schemas as ``docs/schemas/``), so a result read
over HTTP is the file ``export_study`` writes.
"""

import base64
import binascii
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from flex_orchestrator.archive.export import dataset_files, result_files
from flex_orchestrator.errors import SpecError, TagNotFoundError
from flex_orchestrator.orchestrator import MODEL_SPEC, SURROGATE_SPEC
from flex_orchestrator.server.deps import (
    get_orchestrator,
    require_role,
    study_orchestrator,
)
from flex_orchestrator.verify import verify

router = APIRouter(tags=["studies"])

viewer = Depends(require_role("viewer"))
editor = Depends(require_role("editor"))
owner = Depends(require_role("owner"))

SpecKindPath = Literal["model", "surrogate"]
_SPEC_KINDS = {"model": MODEL_SPEC, "surrogate": SURROGATE_SPEC}

# A spec's files, as the client names them (``<sha256[:12]>/<filename>``), and
# how much of them one request may carry.
_FILE_NAME = re.compile(r'[0-9a-f]{12}/[^/\\:*?"<>|\x00-\x1f]+')
MAX_SPEC_FILES = 64 * 2**20
ItemKind = Literal["result", "data", "model_spec", "surrogate_spec", "fit"]


class NewStudy(BaseModel):
    """``POST /studies``."""

    model_config = ConfigDict(extra="forbid")
    name: str


class SpecIn(BaseModel):
    """``POST /studies/{s}/specs/{kind}/{tag}``.

    Attributes:
        spec: The spec, as JSON, each file it references named as in
            ``files``, or by the stored name its ``parent`` has for it.
        parent: The spec it's derived from (model specs).
        spec_format: Its format; detected when omitted.
        assets: More file references, as JSON pointers (model specs).
        files: ``{"<sha256[:12]>/<filename>": base64}`` for each file the
            spec references (model specs; at most 64 MiB in all). It's stored
            as ``assets/<sha256[:12]>-<filename>``, as from Python.
        replace: Replace a stored spec with this tag.
    """

    model_config = ConfigDict(extra="forbid")
    spec: dict[str, Any]
    parent: str | None = None
    spec_format: str | None = None
    assets: list[str] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)
    replace: bool = False


class SpecOut(BaseModel):
    """A stored spec."""

    tag: str
    kind: str
    spec: dict[str, Any]
    spec_hash: str
    spec_format: str
    parent_tag: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DataColumn(BaseModel):
    """One column of an uploaded dataset."""

    model_config = ConfigDict(extra="forbid")
    unit: str | None = None
    values: list[float | None]


class DataIn(BaseModel):
    """``POST /studies/{s}/data/{name}``: a dataset, as the D17 file has it.

    Attributes:
        time_index: Timestamps, ISO 8601; naive means UTC.
        columns: ``{column: {unit, values}}``, one value per timestamp.
        replace: Replace a stored dataset with this name.
    """

    model_config = ConfigDict(extra="forbid")
    time_index: list[datetime]
    columns: dict[str, DataColumn] = Field(min_length=1)
    replace: bool = False


@router.get("/studies", dependencies=[viewer])
def list_studies(orch: Any = Depends(get_orchestrator)) -> dict:
    """The tenant's studies."""
    return {"studies": orch.studies()}


@router.post("/studies", status_code=201, dependencies=[editor])
def create_study(body: NewStudy, orch: Any = Depends(get_orchestrator)) -> dict:
    """Create a study; 409 if it exists."""
    return {"study": orch.create_study(body.name)}


@router.get("/studies/{study}", dependencies=[viewer])
def get_study(study: str, orch: Any = Depends(study_orchestrator)) -> dict:
    """What the study holds: per kind, runs by status and the tags stored."""
    found = verify(orch, [study], samples=0)["studies"][study]
    return {
        "study": study,
        "items": {
            kind: {"runs": v["runs"], "tags": v["tags"]} for kind, v in found.items()
        },
    }


@router.delete("/studies/{study}", status_code=204, dependencies=[owner])
def delete_study(study: str, orch: Any = Depends(get_orchestrator)) -> Response:
    """Delete a study and everything in it, for good (D9)."""
    orch.delete_study(study)
    return Response(status_code=204)


@router.get("/studies/{study}/specs/{kind}", dependencies=[viewer])
def list_specs(kind: SpecKindPath, orch: Any = Depends(study_orchestrator)) -> dict:
    """The tags of the study's specs of one kind, oldest first."""
    return {"tags": [s.tag for s in orch.stored_specs(_SPEC_KINDS[kind])]}


@router.get("/studies/{study}/specs/{kind}/{tag}", dependencies=[viewer])
def get_spec(
    kind: SpecKindPath, tag: str, orch: Any = Depends(study_orchestrator)
) -> SpecOut:
    """A stored spec; 404 if there's none."""
    found = (orch.model_spec if kind == "model" else orch.surrogate_spec)(tag)
    return _spec_out(found)


@router.post(
    "/studies/{study}/specs/{kind}/{tag}", status_code=201, dependencies=[editor]
)
def store_spec(
    kind: SpecKindPath, tag: str, body: SpecIn, orch: Any = Depends(study_orchestrator)
) -> SpecOut:
    """Store a spec; 409 if the tag is taken (unless ``replace``)."""
    if kind == "surrogate":
        if body.files or body.assets:
            raise SpecError("A surrogate spec references no files.")
        found = orch.store_surrogate_spec(
            tag, body.spec, spec_format=body.spec_format, replace=body.replace
        )
        return _spec_out(found)
    # The files go to a scratch folder under their names, where the spec's
    # paths find them; the orchestrator reads and hashes them as it would any.
    with tempfile.TemporaryDirectory(prefix="flexorch-spec-") as scratch:
        _write_files(Path(scratch), body.files)
        found = orch.store_model_spec(
            tag,
            body.spec,
            parent=body.parent,
            base_dir=scratch,
            assets=body.assets,
            spec_format=body.spec_format,
            replace=body.replace,
        )
    return _spec_out(found)


def _write_files(folder: Path, files: dict[str, str]) -> None:
    total = 0
    for name, encoded in files.items():
        if not _FILE_NAME.fullmatch(name) or name.endswith(("/.", "/..")):
            raise SpecError(f"{name!r} isn't a file name (<sha256[:12]>/<file>).")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise SpecError(f"{name!r} isn't base64.") from None
        total += len(data)
        if total > MAX_SPEC_FILES:
            raise SpecError("A spec's files may be at most 64 MiB in all.")
        target = folder / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)


@router.get("/studies/{study}/data", dependencies=[viewer])
def list_data(orch: Any = Depends(study_orchestrator)) -> dict:
    """The study's dataset names, oldest first."""
    return {"datasets": [f.name for f in dataset_files(orch, None)]}


@router.post("/studies/{study}/data/{name}", status_code=201, dependencies=[editor])
def store_data(
    name: str, body: DataIn, orch: Any = Depends(study_orchestrator)
) -> dict:
    """Store a dataset; 409 if the name is taken (unless ``replace``)."""
    index = pd.DatetimeIndex(body.time_index)
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    frame = pd.DataFrame(
        {column: c.values for column, c in body.columns.items()}, index=index
    )
    units = {c: col.unit for c, col in body.columns.items() if col.unit is not None}
    stored = orch.store_data(name, frame, units=units, replace=body.replace)
    return {"name": name, "run_id": stored.run_id, "rows": len(frame)}


@router.get("/studies/{study}/data/{name}", dependencies=[viewer])
def get_data(
    name: str,
    columns: list[str] | None = Query(None),
    orch: Any = Depends(study_orchestrator),
) -> dict:
    """A dataset, as its D17 file (``DatasetFile``); 404 if there's none."""
    [found] = list(dataset_files(orch, {name})) or [None]
    if found is None:
        raise TagNotFoundError(f"Study {orch.study!r} has no dataset {name!r}.")
    if columns:
        missing = sorted(set(columns) - set(found.columns))
        if missing:
            raise TagNotFoundError(f"Dataset {name!r} has no columns {missing}.")
        found.columns = {c: found.columns[c] for c in columns}
    return found.model_dump(mode="json", by_alias=True)


@router.get("/studies/{study}/results", dependencies=[viewer])
def list_results(
    tags: list[str] | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    orch: Any = Depends(study_orchestrator),
) -> dict:
    """The study's results as D17 result files, ``limit`` at a time.

    Returns ``{"results": {tag: ResultFile}, "total", "next"}``; ``next`` is
    the ``offset`` of the next page, or ``null`` after the last.
    """
    stored = [r.tag for r in orch.stored_model_results(tags)]
    page = stored[offset : offset + limit]
    found = {
        f.tag: f.model_dump(mode="json", by_alias=True)
        for f in result_files(orch, set(page))
    }
    following = offset + limit
    return {
        "results": {tag: found[tag] for tag in page if tag in found},
        "total": len(stored),
        "next": following if following < len(stored) else None,
    }


@router.get("/studies/{study}/results/{tag}", dependencies=[viewer])
def get_result(tag: str, orch: Any = Depends(study_orchestrator)) -> dict:
    """One result, as its D17 file; 404 if there's none."""
    [found] = list(result_files(orch, {tag}, require=True))
    return found.model_dump(mode="json", by_alias=True)


@router.delete("/studies/{study}/{kind}/{tag}", status_code=204, dependencies=[owner])
def delete_item(
    kind: ItemKind, tag: str, orch: Any = Depends(study_orchestrator)
) -> Response:
    """Delete an item and every run of it, for good (D9); 404 if there's none."""
    orch.delete(kind, tag)
    return Response(status_code=204)


def _spec_out(found: Any) -> SpecOut:
    return SpecOut(
        tag=found.tag,
        kind=found.kind,
        spec=found.spec,
        spec_hash=found.spec_hash,
        spec_format=found.spec_format,
        parent_tag=found.parent_tag,
        metadata=found.metadata(),
    )
