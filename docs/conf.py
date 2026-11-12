"""Sphinx configuration for flex-orchestrator."""

from importlib.metadata import version as _dist_version
from pathlib import Path

project = "flex-orchestrator"
copyright = "2026, flex-pse contributors"
author = "flex-pse contributors"
release = _dist_version("flex-orchestrator")
version = release

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "myst_parser",
]

source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
exclude_patterns = ["_build", "_tutorials"]
myst_heading_anchors = 3

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {"members": True, "show-inheritance": True}
napoleon_google_docstring = True
napoleon_numpy_docstring = False

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pandas": ("https://pandas.pydata.org/docs/", None),
    "polars": ("https://docs.pola.rs/api/python/stable/", None),
    "pydantic": ("https://docs.pydantic.dev/latest/", None),
    "pyomo": ("https://pyomo.readthedocs.io/en/stable/", None),
    "ray": ("https://docs.ray.io/en/latest/", None),
}

nitpicky = True
nitpick_ignore = [
    # pydantic renders a field's metadata, which has no intersphinx target.
    ("py:class", "FieldInfo"),
    ("py:class", "NoneType"),
]
nitpick_ignore_regex = [
    # Private bases (the strict pydantic base, the export file base, ...),
    # shown by show-inheritance but not documented.
    ("py:class", r"flexorchestrator\..*\._\w+"),
    # pydantic Field constraints render as annotated_types metadata and as
    # "ge=1"-style fragments of the constructor signature.
    ("py:class", r"annotated_types\..*"),
    ("py:class", r"\w+=.*"),
    # flex-pse publishes no intersphinx inventory yet.
    ("py:.*", r"flex(core|ops|design|parameterize)(\..*)?"),
    # polars type hints resolve to internal paths not in its inventory.
    ("py:class", r"polars\..*"),
]

html_theme = "furo"
html_title = f"flex-orchestrator {release}"
html_static_path = ["_static"]
# The tutorials, exported to HTML by the docs workflow
# (`marimo export html tutorials/<name>.py -o docs/_tutorials/tutorials/<name>.html`);
# a local build without them still works, with the links unresolved.
html_extra_path = [p for p in ["_tutorials"] if (Path(__file__).parent / p).is_dir()]
