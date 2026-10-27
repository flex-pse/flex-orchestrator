"""ro_tools: a sample flex-orchestrator plugin.

It adds one command, ``cost_table``, an orchestrator subclass that stores
only the RO plant's values by default and stamps every stored item with the
plugin's version, and (with ``flex-orchestrator[server]``) a route:
``GET /ext/ro_tools/costs?study=...``, the same table over HTTP.

Enable it with ``FLEXORCH_PLUGINS=ro_tools`` (installing it isn't enough), then
``flex-orch cmd cost_table --study my_study``, or from Python::

    from flex_orchestrator.ext import create_orchestrator
    orch = create_orchestrator(plugins=["ro_tools"])
    orch.run_command("cost_table")
"""

from pydantic import BaseModel

from flex_orchestrator import Orchestrator
from flex_orchestrator.ext import Plugin, command

__version__ = "0.1.0"


class CostTableParams(BaseModel):
    """Which value to tabulate."""

    key: str = "costing.aggregate_operating_cost"
    tags: list[str] | None = None


@command(name="cost_table", role="viewer")
def cost_table(orch, params: CostTableParams, progress) -> dict:
    """Tabulate one scalar of every stored result: {tag: value}."""
    return {
        result.tag: result.scalars().get(params.key)
        for result in orch.stored_model_results(params.tags)
    }


class ROOrchestrator(Orchestrator):
    """Stores the RO plant's values by default, and stamps the plugin's version."""

    def default_export_selection(self) -> dict:
        """Store the plant and its cost, not the whole model."""
        return {"include": ["plant.*", "costing.aggregate_operating_cost"]}

    def before_store(self, item) -> None:
        """Refuse scratch results; record the plugin's version on everything."""
        if item.kind == "result" and item.tag.startswith("scratch_"):
            raise ValueError(f"ro_tools doesn't store scratch results ({item.tag!r}).")
        item.metadata["ro_tools_version"] = __version__


def _routers() -> list:
    """The plugin's HTTP routes, if the server is installed (they need FastAPI)."""
    try:
        from fastapi import APIRouter, Depends, Query

        from flex_orchestrator.server.deps import get_orchestrator, require_role
    except ImportError:
        return []
    router = APIRouter()

    # The server mounts this at /ext/ro_tools and checks the token first.
    @router.get("/costs", dependencies=[Depends(require_role("viewer"))])
    def costs(study: str = Query(...), orch=Depends(get_orchestrator)) -> dict:
        orch.select_study(study)
        return orch.run_command("cost_table")

    return [router]


plugin = Plugin(
    name="ro_tools",
    api_version=">=1,<2",
    commands=[cost_table],
    hooks={
        "ro_fake": {
            "build": "flex_orchestrator.testing:fake_build",
            "solve": "flex_orchestrator.testing:fake_solve",
        }
    },
    orchestrator_cls=ROOrchestrator,
    routers=_routers(),
)
