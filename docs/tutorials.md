# Tutorials

Guided [marimo](https://marimo.io) notebooks that walk through what
flex-orchestrator does, with charts of the data, of how it reaches the model,
and of the results. The pages below are each notebook as it ran when these
docs were built.

| Tutorial | Covers |
| --- | --- |
| <a href="tutorials/01_first_result.html">First result</a> | The orchestrator end to end: store data and a model spec, build, solve, store the result, and read it back with its provenance |
| <a href="tutorials/02_model_inputs.html">Model inputs</a> | Setting model variables from stored data: selectors, the window read, lining data up with the time steps, and costing the schedule a plant ran |
| <a href="tutorials/03_surrogates.html">Surrogates</a> | Fitting surrogates from operating data, applying them, and checking them against the true curve |
| <a href="tutorials/04_studies_and_sweeps.html">Studies and sweeps</a> | Study configs: sweep types, dry runs, `product` and `zip`, cases, comparing and exporting results |
| <a href="tutorials/05_design_studies.html">Design studies</a> | Horizons; a demand projection per modeled year, set with `horizon[2030,*]`; how the data was lined up with each horizon's time steps; sizing; a sweep over demand scenarios; rebuilding a solved model |

## Running them yourself

The tutorials live in the repository's
[`tutorials/`](https://github.com/flex-pse/flex-orchestrator/tree/main/tutorials)
folder; they aren't part of the pip package. From a clone, with flex-pse
installed:

```bash
pip install -e ".[tutorials]"
marimo edit tutorials/01_first_result.py
```

Each one stores in a temporary folder (`FileBackend`), so it needs no
Acquirium. Move a slider and the cells that depend on it run again.
