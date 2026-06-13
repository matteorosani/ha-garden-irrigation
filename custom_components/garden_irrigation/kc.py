"""
Crop coefficient (Kc) interpolation over the growing season.

FAO-56 models the crop coefficient as a piecewise curve across four
growth stages. Given a crop definition (from crops.json) and the number
of days since planting, this module returns the correct Kc for that day.

Growth stages
-------------
  Initial   (l_ini days) : Kc = kc_ini  [flat — seedling, sparse cover]
  Development (l_dev days): Kc rises linearly from kc_ini → kc_mid
  Mid-season  (l_mid days): Kc = kc_mid  [flat — full canopy, peak demand]
  Late season (l_late days): Kc falls linearly from kc_mid → kc_end

After the total season length the crop is considered harvested and Kc
stays at kc_end (the caller can choose to stop irrigation instead).

Multiple crops in one zone
--------------------------
When a zone contains several crops, ``kc_for_zone`` averages the daily
Kc across all of them. This is a deliberate simplification: the valve
serves all roots equally so watering for the average need is the best
single-valve strategy.

Reference
---------
Allen, R.G. et al. (1998). FAO Irrigation and Drainage Paper 56.
Table 11 — Crop coefficients and growth stage durations.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

# ── Data model ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CropDefinition:
    """
    All the FAO-56 parameters for one crop, loaded from crops.json.

    Attributes
    ----------
    id      : unique identifier string, e.g. "tomato"
    name    : human-readable label, e.g. "Tomato"
    kc_ini  : Kc during initial (seedling) stage
    kc_mid  : Kc at peak mid-season
    kc_end  : Kc at end of season / harvest
    l_ini   : length of initial stage in days
    l_dev   : length of development stage in days
    l_mid   : length of mid-season stage in days
    l_late  : length of late-season stage in days
    """

    id: str
    name: str
    kc_ini: float
    kc_mid: float
    kc_end: float
    l_ini: int
    l_dev: int
    l_mid: int
    l_late: int

    @property
    def total_days(self) -> int:
        """Total season length in days."""
        return self.l_ini + self.l_dev + self.l_mid + self.l_late


# ── Crop library loader ────────────────────────────────────────────────────────

_CROPS_FILE = os.path.join(os.path.dirname(__file__), "crops.json")
_crop_registry: dict[str, CropDefinition] | None = None
_user_crops_file: str | None = None


def set_user_crops_file(path: str | None) -> None:
    """
    Register the path to the user's custom crops file.

    Called once from async_setup_entry after HA's config directory is known.
    Pass None to disable user crops. Invalidates the cache so the next
    load_crops() call re-reads both files.
    """
    global _user_crops_file, _crop_registry
    _user_crops_file = path
    _crop_registry = None


def _parse_crops_file(path: str) -> dict[str, CropDefinition]:
    """Read one crops JSON file and return a registry dict."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    registry: dict[str, CropDefinition] = {}
    for entry in raw["crops"]:
        crop = CropDefinition(
            id=entry["id"],
            name=entry["name"],
            kc_ini=float(entry["kc_ini"]),
            kc_mid=float(entry["kc_mid"]),
            kc_end=float(entry["kc_end"]),
            l_ini=int(entry["l_ini"]),
            l_dev=int(entry["l_dev"]),
            l_mid=int(entry["l_mid"]),
            l_late=int(entry["l_late"]),
        )
        registry[crop.id] = crop
    return registry


def load_crops(path: str = _CROPS_FILE) -> dict[str, CropDefinition]:
    """
    Load the crop registry, merging bundled and user-defined crops.

    Load order (later entries win on duplicate IDs):
      1. Bundled crops.json shipped with the integration.
      2. User file registered via set_user_crops_file() — lives in the HA
         config directory so it survives integration updates.

    A user crop with the same id as a bundled crop overrides it, which lets
    you calibrate Kc or stage durations to your local conditions.

    Parameters
    ----------
    path : override the bundled file — used in tests only.
    """
    global _crop_registry
    if _crop_registry is not None and path == _CROPS_FILE:
        return _crop_registry

    registry = _parse_crops_file(path)

    if path == _CROPS_FILE and _user_crops_file and os.path.exists(_user_crops_file):
        try:
            registry.update(_parse_crops_file(_user_crops_file))
        except (json.JSONDecodeError, KeyError, ValueError) as err:
            import logging

            logging.getLogger(__name__).error(
                "garden_irrigation: failed to load user crops from %s - %s",
                _user_crops_file,
                err,
            )

    if path == _CROPS_FILE:
        _crop_registry = registry
    return registry


def get_crop(crop_id: str) -> CropDefinition:
    """
    Return a single CropDefinition by ID, raising KeyError if unknown.

    This is what the config flow will call to validate user selections.
    """
    return load_crops()[crop_id]


def available_crops() -> list[CropDefinition]:
    """All crops in the library, sorted alphabetically by name."""
    return sorted(load_crops().values(), key=lambda c: c.name)


# ── Kc interpolation ───────────────────────────────────────────────────────────


def kc_for_day(crop: CropDefinition, days_since_planting: int) -> float:
    """
    Return the crop coefficient Kc for a single crop on a given day.

    Parameters
    ----------
    crop                : CropDefinition loaded from the registry
    days_since_planting : 0 on planting day; negative values treated as 0

    Returns
    -------
    Kc  (dimensionless, always > 0)

    Stage boundaries (cumulative days from planting)
    -------------------------------------------------
    0            → l_ini          : initial  (Kc = kc_ini, flat)
    l_ini        → l_ini+l_dev    : development (Kc rises linearly)
    l_ini+l_dev  → ...+l_mid      : mid-season  (Kc = kc_mid, flat)
    ...+l_mid    → total_days      : late-season (Kc falls linearly)
    > total_days                   : season over (Kc = kc_end)
    """
    d = max(0, days_since_planting)

    # Cumulative stage boundary days
    end_ini = crop.l_ini
    end_dev = end_ini + crop.l_dev
    end_mid = end_dev + crop.l_mid
    end_late = end_mid + crop.l_late  # == crop.total_days

    if d < end_ini:
        # Initial stage — flat
        return crop.kc_ini

    if d < end_dev:
        # Development stage — linear ramp up
        # How far through this stage are we? (0.0 = just started, 1.0 = end)
        progress = (d - end_ini) / crop.l_dev
        return _lerp(crop.kc_ini, crop.kc_mid, progress)

    if d < end_mid:
        # Mid-season — flat at peak
        return crop.kc_mid

    if d < end_late:
        # Late-season — linear ramp down
        progress = (d - end_mid) / crop.l_late
        return _lerp(crop.kc_mid, crop.kc_end, progress)

    # Past harvest date — return kc_end
    # The irrigator layer can decide whether to stop watering entirely.
    return crop.kc_end


def kc_for_zone(
    crop_ids: Sequence[str],
    planting_date: date,
    today: date,
) -> float:
    """
    Effective Kc for a zone containing one or more crops on a given date.

    For a mixed zone the result is the simple average of each crop's Kc
    on that day. This is intentional: a single valve waters all roots
    equally, so watering for the mean demand is the rational strategy.

    Parameters
    ----------
    crop_ids      : list of crop ID strings from crops.json
    planting_date : the date the crops were planted
    today         : the date for which to compute Kc

    Returns
    -------
    Average Kc across all crops (> 0).
    Falls back to 1.0 if crop_ids is empty (safe default — no under-watering).
    """
    if not crop_ids:
        return 1.0

    days = (today - planting_date).days
    registry = load_crops()

    kc_values = [kc_for_day(registry[cid], days) for cid in crop_ids if cid in registry]

    if not kc_values:
        return 1.0

    return sum(kc_values) / len(kc_values)


# ── Internal helpers ───────────────────────────────────────────────────────────


def _lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation: returns a + t*(b-a), t clamped to [0, 1]."""
    t = max(0.0, min(1.0, t))
    return a + t * (b - a)
