# Tutorials

Guided [marimo](https://marimo.io) notebooks that show what flex-orchestrator
does, with charts of the data, of how it reaches the model, and of the
results. They live here on GitHub and in the docs (Tutorials), with their
outputs; they aren't part of the pip package.

| Notebook | Covers |
| --- | --- |
| [`01_first_result.py`](01_first_result.py) | The orchestrator end to end: store data and a model spec, build, solve, store the result, read it back with its provenance; a spec is plain data, so a tank-size variant is one more stored spec |
| [`02_model_inputs.py`](02_model_inputs.py) | Setting model variables from stored data: selectors and `rename`, the window read (`offset`, `timezone`), lining data up with the time steps (`resample`, `gaps`, `reduce`), and costing the schedule a plant actually ran |
| [`03_surrogates.py`](03_surrogates.py) | Fitting surrogates from operating data, storing and applying them, and checking each model's energy against the true curve; the same as a study's `fits` and `surrogate` sweep |
| [`04_studies_and_sweeps.py`](04_studies_and_sweeps.py) | Study configs: the sweep types, planning with a dry run, `product` and `zip`, cases, comparing results, and exporting a study |
| [`05_design_studies.py`](05_design_studies.py) | Horizons; a demand projection per modeled year, set with `horizon[2030,*]`; how the data was lined up with each horizon's time steps; sizing; a sweep over demand scenarios; rebuilding a solved model |

## Running one

From a clone, with flex-pse installed (see the docs' Installation page):

```bash
pip install -e ".[tutorials]"
marimo edit tutorials/01_first_result.py
```

`marimo edit` opens the notebook in the browser; move a slider and the cells
that depend on it run again. Each notebook stores in a temporary folder
(`FileBackend`), so none needs Acquirium.

A notebook is a plain Python file, so it also runs as a script:
`python tutorials/05_design_studies.py` runs every cell, which is how the
tests check them. `marimo export html tutorials/05_design_studies.py -o out.html`
saves it with its outputs, as the docs publish it.

## Files

- `NN_<topic>.py`: the notebooks.
- `tutorial_data.py`: what they share: the RO plant of tutorials 1 to 4 (its
  model, operating data and tariff prices) and tutorial 5's demand data.
- `assets/`: the design flowsheet and the RO plant's tariff file.
