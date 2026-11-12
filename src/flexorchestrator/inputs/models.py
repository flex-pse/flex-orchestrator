"""``model_inputs`` entries: stored data a model variable is set from.

An entry is a fit selector (``dataset``, ``result``, ``ref_uris``,
``streams``, ``resolve`` or ``sparql``, with ``start``, ``end``, ``units`` and
``rename``) plus how its data is lined up with the model's time steps. After
``rename``, each column's name is the model path it sets.

Plain pydantic, so a study config carries it as data.
"""

import zoneinfo
from typing import Literal

import pandas as pd
from pydantic import Field, field_validator, model_validator

from flexorchestrator.fitting.models import Selector, _Strict

Agg = Literal["mean", "sum", "min", "max", "median", "first", "last"]
Gaps = Literal["interpolate", "ffill", "zero", "error"]
Reduce = Literal["mean", "last", "first", "min", "max", "median"]
Direction = Literal["forward", "backward"]


class InputResample(_Strict):
    """How samples become one value per time step.

    A time step at ``t`` with length ``dt`` takes the samples of one interval
    and combines them with ``agg``:

    - ``forward`` (the default): ``[t, t + dt)``, the interval starting at
      ``t``. This is what flex-pse means by a time step (its time points are
      interval starts, and it costs a rate as ``rate[t] * dt``), so it's the
      one for rates and costs. A sample stamped ``s`` stands for the interval
      that starts at ``s``.
    - ``backward``: ``(t - dt, t]``, the interval ending at ``t``, for a value
      that sums up what happened up to ``t``. A sample stamped ``s`` stands
      for the interval that ends at ``s``, and the first step reads data from
      before the horizon.

    Data stamped the other way (a meter that stamps 10:00 to 10:15 as 10:15,
    read ``forward``) is shifted onto its interval's start with ``offset``
    (``"15min"``).

    With ``agg: sum`` and data coarser than the steps (hourly kWh on
    15-minute steps), each sample's amount is split evenly over the steps its
    interval covers. That needs the data's spacing to be a whole number of
    steps.

    Attributes:
        agg: How the samples in one step combine.
        window: Which interval a step takes: ``forward`` or ``backward``.
    """

    agg: Agg = "mean"
    window: Direction = "forward"


class ModelInput(Selector):
    """One ``model_inputs`` entry: a selector, then its alignment.

    Attributes:
        timezone: The facility's time zone (an IANA name such as
            ``"America/Los_Angeles"``). The model runs on that site's wall
            clock, so stored UTC timestamps are converted to it before they're
            lined up with the model's (naive) time steps. ``UTC`` by default:
            data stored in UTC means a model in UTC.
        resample: How the samples in one time step combine; ``mean`` by
            default.
        gaps: A time step with no value is filled by ``interpolate`` (in
            time, the default), ``ffill`` (the last value carried forward) or
            ``zero``; ``error`` refuses any gap.
        limit: The longest run of missing steps ``interpolate`` or ``ffill``
            fills; a longer one fails.
        reduce: Set the variable to one number instead of a series: this
            statistic of the data over the model's horizon.
        offset: Read the data this far from the model's horizon, as a pandas
            offset (``"-365D"``: last year's data for this horizon).
        data_years: ``{modeled year: data year}``: a horizon in a modeled year
            reads the same calendar dates in the data year, before ``offset``.
            A design study's 2035 horizons can read 2025's data with
            ``{2035: 2025}``. A year not listed reads its own dates. A modeled
            29 February reads 28 February when the data year has none.
    """

    timezone: str = "UTC"
    resample: InputResample | None = None
    gaps: Gaps = "interpolate"
    limit: int | None = Field(default=None, ge=1)
    reduce: Reduce | None = None
    offset: str | None = None
    data_years: dict[int, int] | None = None

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @field_validator("offset")
    @classmethod
    def _an_offset(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                pd.Timedelta(value)
            except ValueError as exc:
                raise ValueError(f"offset {value!r} isn't a duration") from exc
        return value

    @model_validator(mode="after")
    def _reduce_alone(self) -> "ModelInput":
        if self.reduce is not None:
            given = {"resample", "gaps", "limit"} & self.model_fields_set
            if given:
                raise ValueError(
                    f"reduce gives one number, so {sorted(given)} don't apply"
                )
        return self


class InputSet(_Strict):
    """One value of a ``model_inputs`` sweep: a labelled set of entries.

    Attributes:
        label: What ``output_name`` renders the set as.
        inputs: The entries a task with this value uses.
    """

    label: str
    inputs: list[ModelInput] = Field(min_length=1)
