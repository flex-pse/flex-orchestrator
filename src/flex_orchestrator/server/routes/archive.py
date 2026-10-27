"""Export and import over HTTP (PLAN.md §4, D17).

``GET /studies/{s}/export`` sends the study's D17 export folder as a zip;
``POST /studies/import`` takes such a zip and imports it on the job queue,
checking every file's checksum as ``import_study`` does.
"""

import io
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from flex_orchestrator.errors import ArchiveError
from flex_orchestrator.server.auth import Principal
from flex_orchestrator.server.deps import (
    current_principal,
    get_jobs,
    require_role,
    study_orchestrator,
)
from flex_orchestrator.server.routes.commands import _accepted

router = APIRouter(tags=["export"])

MAX_UPLOAD = 2 * 1024**3
"""int: The largest export zip ``POST /studies/import`` takes, in bytes."""


@router.get("/studies/{study}/export", dependencies=[Depends(require_role("viewer"))])
def export_study(study: str, orch: Any = Depends(study_orchestrator)) -> FileResponse:
    """The study's D17 export, as ``<study>.zip``."""
    work = Path(tempfile.mkdtemp(prefix="flexorch-export-"))
    folder = orch.export_study(work / study).path
    archive = shutil.make_archive(str(work / study), "zip", root_dir=folder)
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=f"{study}.zip",
        background=BackgroundTask(shutil.rmtree, work, ignore_errors=True),
    )


@router.get(
    "/studies/{study}/results/{tag}/export",
    dependencies=[Depends(require_role("viewer"))],
)
def export_result(
    study: str, tag: str, orch: Any = Depends(study_orchestrator)
) -> FileResponse:
    """One result's D17 file, as ``<tag>.json``."""
    work = Path(tempfile.mkdtemp(prefix="flexorch-export-"))
    path = orch.export_result(tag, work / f"{tag}.json")
    return FileResponse(
        path,
        media_type="application/json",
        filename=f"{tag}.json",
        background=BackgroundTask(shutil.rmtree, work, ignore_errors=True),
    )


@router.post("/studies/import", dependencies=[Depends(require_role("editor"))])
def import_study(
    file: UploadFile = File(..., description="a zip of a D17 export folder"),
    name: str | None = Form(None),
    on_existing: Literal["error", "skip"] = Form("error"),
    principal: Principal = Depends(current_principal),
    jobs: Any = Depends(get_jobs),
) -> JSONResponse:
    """Import an export zip as a study: ``202 {"job_id"}``.

    The zip is unpacked now (refused if it's too large or names a path outside
    itself); the import, with its checksum checks, runs on the job queue.
    """
    work = Path(tempfile.mkdtemp(prefix="flexorch-import-"))
    try:
        folder = _unpack(file, work)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise

    def run(orch: Any, progress: Any) -> dict:
        try:
            report = orch.import_study(folder, name=name, on_existing=on_existing)
            return {
                "study": report.study,
                "counts": report.counts,
                "skipped": report.skipped,
            }
        finally:
            shutil.rmtree(work, ignore_errors=True)

    params = {"file": file.filename, "name": name, "on_existing": on_existing}
    return _accepted(
        jobs.submit_call("import_study", run, params, study=None, user=principal.user)
    )


def _unpack(upload: UploadFile, work: Path) -> Path:
    data = upload.file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise ArchiveError(f"The upload is larger than {MAX_UPLOAD} bytes.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"The upload isn't a zip: {exc}") from None
    root = (work / "export").resolve()
    with archive:
        for member in archive.infolist():
            target = (root / member.filename).resolve()
            if not target.is_relative_to(root):
                raise ArchiveError(
                    f"The zip names a path outside itself: {member.filename!r}."
                )
        archive.extractall(root)
    # A zip of the folder itself (export/<study>/...) or of its contents.
    entries = [p for p in root.iterdir()]
    if len(entries) == 1 and entries[0].is_dir() and not (root / "study.json").exists():
        return entries[0]
    return root
