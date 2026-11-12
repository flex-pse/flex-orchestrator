# flex-orchestrator

flex-orchestrator runs [flex-pse](https://github.com/flex-pse/flex-pse)
process-model studies and stores everything they need and produce in
[Acquirium](https://github.com/DataDrivenCPS/acquirium): model specs,
operating data, surrogate fits and solved results.

A **study** is a model spec plus sweeps: of surrogates, variables,
configuration options or hand-picked cases. Each solve is stored as a **run**
that you can query, compare, export to disk, and import elsewhere.

flex-orchestrator is a library. A script, a notebook or a host application
calls it directly. Serving it over HTTP, and handling users and
authentication, is the host application's job.

```{toctree}
:maxdepth: 2
:caption: Getting started

getting_started/installation
getting_started/quick_start
```

```{toctree}
:maxdepth: 1
:caption: Tutorials

tutorials
```

```{toctree}
:maxdepth: 2
:caption: Explanation

explanation/connecting
explanation/studies
explanation/storage
explanation/parallel
explanation/embedding
explanation/extending
```

```{toctree}
:maxdepth: 1
:caption: Design decisions

decisions/0001-ray-execution
```

```{toctree}
:maxdepth: 2
:caption: Reference

reference/cli
reference/orchestrator
reference/connection
reference/studies
reference/fitting
reference/storage
reference/export
reference/extending
reference/testing
reference/errors
reference/schemas
```

```{toctree}
:maxdepth: 1
:caption: Release notes

changelog
```
