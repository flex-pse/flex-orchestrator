"""Model inputs: stored data lined up with a model's time steps.

The resolver alone, on memory and files, with the time steps given directly;
``test_inputs_study.py`` runs them through studies and builds.
"""

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from flexorchestrator.errors import ModelInputError
from flexorchestrator.fitting import selectors
from flexorchestrator.inputs import ModelInput
from flexorchestrator.inputs.resolve import resolve
from flexorchestrator.inputs.scopes import grids_of, scope_of, targets

HOURS = pd.date_range("2025-07-07", periods=72, freq="h", tz="UTC")
GRID = pd.date_range("2025-07-08", periods=8, freq="15min")  # 00:00 to 01:45


@pytest.fixture
def stored(orch):
    """``ops``: hourly demand 0, 1, 2, ... (m^3/hr) and a unitless count."""
    frame = pd.DataFrame(
        {"demand": np.arange(72.0), "count": np.arange(72.0) * 10}, HOURS
    )
    orch.store_data("ops", frame, units={"demand": "m^3/hr"})
    return orch


def entry(**fields):
    return ModelInput(dataset="ops", columns=["demand"], **fields)


def test_hourly_data_fills_every_step(stored):
    found = resolve(stored, [entry(rename={"demand": "plant.d"})], GRID)
    value = found.values["plant.d"]
    assert value["units"] == "m^3/hr"
    # 24 at 00:00 and 25 at 01:00 (UTC); the quarter hours between are filled.
    assert value["value"] == [24.0, 24.25, 24.5, 24.75, 25.0, 25.25, 25.5, 25.75]
    [record] = found.provenance
    assert record["paths"] == ["plant.d"]
    assert record["filled"] == {"plant.d": 6}
    assert record["resolved"]["dataset"] == "ops" and record["resolved"]["run_id"]
    assert record["window"] == {
        "start": "2025-07-08 00:00:00",
        "end": "2025-07-08 02:00:00",
    }
    assert record["data_hash"].startswith("sha256:")
    assert "scope" not in record  # one time axis: records as they always were


def test_a_column_without_a_unit_is_a_plain_list(stored):
    found = resolve(
        stored, [ModelInput(dataset="ops", columns=["count"], gaps="ffill")], GRID
    )
    assert found.values["count"] == [240.0] * 4 + [250.0] * 4


def test_the_facility_time_zone(stored):
    # 00:00 in Los Angeles (PDT) is 07:00 UTC, when the demand was 31.
    found = resolve(stored, [entry(timezone="America/Los_Angeles")], GRID)
    assert found.values["demand"]["value"][0] == 31.0


def test_daylight_saving_days(orch):
    stamps = pd.date_range("2025-03-08", "2025-11-04", freq="h", tz="UTC")
    orch.store_data(
        "dst", pd.DataFrame({"v": np.arange(len(stamps), dtype=float)}, stamps)
    )
    zone = "America/Los_Angeles"
    spring = pd.date_range("2025-03-09", periods=24, freq="h")
    found = resolve(orch, [ModelInput(dataset="dst", timezone=zone)], spring)
    values = found.values["v"]
    # 02:00 doesn't exist that day: interpolated between 01:00 and 03:00.
    assert values[2] == (values[1] + values[3]) / 2
    assert found.provenance[0]["filled"] == {"v": 1}
    autumn = pd.date_range("2025-11-02", periods=24, freq="h")
    found = resolve(orch, [ModelInput(dataset="dst", timezone=zone)], autumn)
    values = found.values["v"]
    # 01:00 happens twice; its two samples average into one step.
    assert values[1] == values[0] + 1.5
    assert values[2] == values[0] + 3


def test_resample_aggregates_onto_a_coarser_step(stored):
    grid = pd.date_range("2025-07-08", periods=2, freq="4h")
    mean = resolve(stored, [entry()], grid).values["demand"]["value"]
    total = resolve(stored, [entry(resample={"agg": "sum"})], grid)
    assert mean == [25.5, 29.5]
    assert total.values["demand"]["value"] == [102.0, 118.0]


def test_a_backward_step_takes_the_interval_ending_at_it(stored):
    grid = pd.date_range("2025-07-08", periods=2, freq="4h")
    backward = {"agg": "mean", "window": "backward"}
    found = resolve(stored, [entry(resample=backward)], grid)
    # 00:00 takes 21:00 to 00:00 (21, 22, 23, 24); forward took 24 to 27.
    assert found.values["demand"]["value"] == [22.5, 26.5]
    assert found.provenance[0]["window"] == {
        "start": "2025-07-07 20:00:00",
        "end": "2025-07-08 04:00:00",
    }
    total = resolve(stored, [entry(resample={**backward, "agg": "sum"})], grid)
    assert total.values["demand"]["value"] == [90.0, 106.0]


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        # The sample at 00:00 (24) covers 00:00 to 01:00: a quarter on each step.
        ("forward", [6.0] * 4 + [6.25] * 4),
        # The sample at 01:00 (25) covers 00:00 to 01:00, ending at it.
        ("backward", [6.0] + [6.25] * 4 + [6.5] * 3),
    ],
)
def test_a_sum_of_coarser_data_is_split_over_the_steps(stored, window, expected):
    resample = {"agg": "sum", "window": window}
    found = resolve(stored, [entry(resample=resample)], GRID)
    assert found.values["demand"]["value"] == expected
    assert found.provenance[0]["filled"] == {"demand": 0}


def test_a_sum_splits_only_over_whole_steps(stored):
    grid = pd.date_range("2025-07-08", periods=3, freq="40min")
    with pytest.raises(ModelInputError, match="whole number of steps"):
        resolve(stored, [entry(resample={"agg": "sum"})], grid)


@pytest.mark.parametrize(
    ("gaps", "expected"),
    [
        ("interpolate", [24.0, 24.5, 25.0]),
        ("ffill", [24.0, 24.0, 25.0]),
        ("zero", [24.0, 0.0, 25.0]),
    ],
)
def test_gap_rules(stored, gaps, expected):
    grid = pd.date_range("2025-07-08", periods=3, freq="30min")
    found = resolve(stored, [entry(gaps=gaps)], grid)
    assert found.values["demand"]["value"] == expected


def test_gaps_that_cant_be_filled(stored):
    with pytest.raises(ModelInputError, match="no value at 6 of the model's 8"):
        resolve(stored, [entry(gaps="error")], GRID)
    with pytest.raises(ModelInputError, match="with limit 2"):
        resolve(stored, [entry(limit=2)], GRID)
    after = pd.date_range("2025-07-09T23:00", periods=4, freq="30min")
    with pytest.raises(ModelInputError, match="no value at 3 of the model's 4"):
        resolve(stored, [entry()], after)  # 23:00 is the last sample


def test_reduce_gives_one_number(stored):
    last = resolve(stored, [entry(reduce="last")], GRID).values["demand"]
    mean = resolve(stored, [entry(reduce="mean")], GRID).values["demand"]
    assert last == {"value": 25.0, "units": "m^3/hr"}
    assert mean["value"] == 24.5
    everything = resolve(stored, [entry(reduce="max")], None)
    assert everything.values["demand"]["value"] == 71.0
    assert everything.provenance[0]["window"] is None


def test_a_model_without_time_steps_takes_only_reduced_inputs(stored):
    with pytest.raises(ModelInputError, match="no time axis"):
        resolve(stored, [entry()], None)


def test_offset_reads_another_window(stored):
    found = resolve(stored, [entry(offset="-1D", reduce="first")], GRID)
    assert found.values["demand"]["value"] == 0.0
    assert found.provenance[0]["window"]["start"] == "2025-07-07 00:00:00"


def test_units_convert(stored):
    found = resolve(stored, [entry(units="m^3/s", reduce="first")], GRID)
    assert found.values["demand"]["value"] == pytest.approx(24 / 3600)
    assert found.values["demand"]["units"] == "m^3/s"


def test_paths_set_once_per_list_and_swept_entries_replace(stored):
    two = [entry(rename={"demand": "p"}), entry(rename={"demand": "p"}, reduce="last")]
    with pytest.raises(ModelInputError, match=r"both set \['p'\]"):
        resolve(stored, two, GRID)
    base = [
        ModelInput(dataset="ops", columns=["demand", "count"], rename={"demand": "p"})
    ]
    found = resolve(
        stored, base, GRID, swept=[entry(rename={"demand": "p"}, reduce="last")]
    )
    assert found.values["p"]["value"] == 25.0
    assert [r["paths"] for r in found.provenance] == [["count"], ["p"]]


def test_a_selector_that_matches_nothing(stored):
    with pytest.raises(
        ModelInputError, match="Model input: .*no dataset|Model input: "
    ):
        resolve(stored, [ModelInput(dataset="nope")], GRID)
    later = pd.date_range("2030-01-01", periods=2, freq="h")
    with pytest.raises(ModelInputError, match="matched no rows"):
        resolve(stored, [entry()], later)


def test_each_distinct_selection_is_read_once(stored, monkeypatch):
    calls = []
    original = selectors.load
    monkeypatch.setattr(
        selectors, "load", lambda *a, **k: calls.append(a[1]) or original(*a, **k)
    )
    cache = {}
    for _ in range(3):
        resolve(stored, [entry(), entry(rename={"demand": "x"})], GRID, cache=cache)
    assert len(calls) == 2  # the rename is part of the selection


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"timezone": "Mars/Olympus"}, "unknown time zone"),
        ({"offset": "a while"}, "isn't a duration"),
        ({"reduce": "last", "gaps": "zero"}, "don't apply"),
        ({"limit": 0}, "greater than or equal to 1"),
        ({"gaps": "guess"}, "Input should be"),
        ({"resample": {"window": "sideways"}}, "Input should be"),
    ],
)
@pytest.mark.unit
def test_invalid_entries(fields, message):
    with pytest.raises(ValidationError, match=message):
        entry(**fields)


# Several time blocks: each path lines up with the nearest one above it.

TWO = {
    "plant_a": GRID,  # 8 July, quarter hours
    "plant_b": pd.date_range("2025-07-09", periods=2, freq="h"),
}


@pytest.mark.unit
def test_a_path_belongs_to_the_nearest_time_block_above_it():
    scopes = ["", "plant", "plant.unit[1]"]
    assert scope_of("plant.unit[1].x", scopes) == "plant.unit[1]"
    assert scope_of("plant.unit[2].x", scopes) == "plant"
    assert scope_of("other.x", scopes) == ""
    assert scope_of("plantx.y", ["plant"]) is None  # a longer name isn't a child
    assert targets("x", ["a", "b"]) == [("a", "a.x"), ("b", "b.x")]
    assert targets("a.x", ["a", "b"]) == [("a", "a.x")]
    # A * in a time block's index matches any value there; one in the
    # variable's own index is left alone.
    horizons = ["h[2030,1]", "h[2030,7]", "h[2035,1]", "h[2035,7]"]
    assert targets("h[2030,*].v[*,Liq]", horizons) == [
        ("h[2030,1]", "h[2030,1].v[*,Liq]"),
        ("h[2030,7]", "h[2030,7].v[*,Liq]"),
    ]
    assert [s for s, _ in targets("h[*,7].v", horizons)] == ["h[2030,7]", "h[2035,7]"]
    assert len(targets("v[*,Liq]", horizons)) == 4  # under none: every horizon
    with pytest.raises(ModelInputError, match="matches no time block"):
        targets("h[2040,*].v", horizons)
    assert targets("u[*].x", ["a"]) == [("a", "a.u[*].x")]  # not a time block
    assert grids_of(None) == {"": None}
    assert list(grids_of(GRID)) == [""]
    with pytest.raises(ModelInputError, match="time_grid hook"):
        grids_of({})


def test_each_path_is_lined_up_with_its_own_time_block(stored):
    count = ModelInput(dataset="ops", columns=["count"], rename={"count": "plant_b.c"})
    found = resolve(stored, [entry(rename={"demand": "plant_a.d"}), count], TWO)
    assert found.values["plant_a.d"]["value"][:2] == [24.0, 24.25]
    assert found.values["plant_b.c"] == [480.0, 490.0]
    records = [(r["scope"], r["paths"], r["window"]) for r in found.provenance]
    assert records == [
        (
            "plant_a",
            ["plant_a.d"],
            {"start": "2025-07-08 00:00:00", "end": "2025-07-08 02:00:00"},
        ),
        (
            "plant_b",
            ["plant_b.c"],
            {"start": "2025-07-09 00:00:00", "end": "2025-07-09 02:00:00"},
        ),
    ]


def test_a_path_under_no_time_block_is_set_in_each(stored):
    found = resolve(stored, [entry(rename={"demand": "d"}, reduce="first")], TWO)
    assert {path: v["value"] for path, v in found.values.items()} == {
        "plant_a.d": 24.0,  # 8 July 00:00
        "plant_b.d": 48.0,  # 9 July 00:00
    }
    assert found.requested == {"plant_a.d": "d", "plant_b.d": "d"}
    assert [r["scope"] for r in found.provenance] == ["plant_a", "plant_b"]
    # A swept entry for the path replaces it in every time block.
    swept = resolve(
        stored,
        [entry(rename={"demand": "d"}, reduce="first")],
        TWO,
        swept=[entry(rename={"demand": "d"}, reduce="last")],
    )
    assert [r["input"]["reduce"] for r in swept.provenance] == ["last", "last"]
    assert swept.values["plant_b.d"]["value"] == 49.0


def test_data_years_read_another_years_dates(stored):
    later = pd.date_range("2030-07-08", periods=2, freq="h")
    found = resolve(stored, [entry(data_years={2030: 2025})], later)
    assert found.values["demand"]["value"] == [24.0, 25.0]
    assert found.provenance[0]["window"]["start"] == "2025-07-08 00:00:00"
    # Then offset shifts within the data year.
    shifted = resolve(stored, [entry(data_years={2030: 2025}, offset="-1D")], later)
    assert shifted.values["demand"]["value"] == [0.0, 1.0]
    # A year that isn't listed reads its own dates, which have no data.
    with pytest.raises(ModelInputError, match="matched no rows"):
        resolve(stored, [entry(data_years={2035: 2025})], later)


def test_an_entry_reads_only_the_time_blocks_it_sets(stored):
    # The first block runs in 2030, when there's no data; the entry sets only
    # the second, so 2030's window is never read.
    grids = {
        "h[2030]": pd.date_range("2030-07-08", periods=2, freq="h"),
        "h[2025]": GRID,
    }
    found = resolve(stored, [entry(rename={"demand": "h[2025].d"})], grids)
    assert list(found.values) == ["h[2025].d"]
    assert [r["scope"] for r in found.provenance] == ["h[2025]"]
    wild = resolve(
        stored,
        [entry(rename={"demand": "h[*].d"}, reduce="first")],
        {"h[2025]": GRID, "x[2030]": grids["h[2030]"]},
    )
    assert list(wild.values) == ["h[2025].d"]
