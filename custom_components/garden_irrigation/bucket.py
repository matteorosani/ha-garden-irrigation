"""
Water balance bucket model.

Tracks soil moisture as a virtual "bucket" measured in millimetres of water
depth. Each day the bucket is updated with the balance between rainfall
received and crop water consumption (ET₀ x Kc). When the bucket falls below
a configurable threshold the zone needs irrigation; watering refills it toward
the maximum capacity.

Units
-----
All quantities are in mm (millimetres of water depth).
  1 mm over 1 m² = 1 litre of water.
  Conversion from litres: water_mm = volume_litres / area_m²

The bucket itself is unit-agnostic with respect to flow rate and zone area —
those conversions happen in the irrigator layer (irrigator.py).

Persistence
-----------
``WaterBucket`` is a plain Python object. Saving/loading its state to disk is
handled by ``store.py``. The ``to_dict`` / ``from_dict`` helpers define the
serialisation contract between the two modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── Config ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BucketConfig:
    """
    Immutable configuration for a single zone's water bucket.

    Attributes
    ----------
    max_capacity  : Maximum soil water storage [mm].
                    Typical values: 20-40 mm for a 30 cm deep vegetable bed.
    low_threshold : Trigger irrigation when bucket drops below this [mm].
                    A common starting point is 50 % of max_capacity.

    """

    max_capacity: float
    low_threshold: float

    def __post_init__(self) -> None:
        if self.max_capacity <= 0:
            raise ValueError(f"max_capacity must be > 0, got {self.max_capacity}")
        if not (0 <= self.low_threshold <= self.max_capacity):
            raise ValueError(
                f"low_threshold must be in [0, max_capacity], "
                f"got {self.low_threshold} (max={self.max_capacity})"
            )


# ── Daily result ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DailyResult:
    """
    Output of a single ``WaterBucket.update()`` call.

    Exposes the individual components of the daily water balance for use
    by sensor entities and logging. All values in mm.

    Attributes
    ----------
    et_crop_mm   : Crop water consumption = ET₀ x Kc  [mm]
    rain_mm      : Effective rainfall applied to bucket [mm]
    net_change   : rain_mm - et_crop_mm (positive = soil gained water)
    level_before : Bucket level at the start of the day  [mm]
    level_after  : Bucket level after the update          [mm]
                   (may differ from level_before + net_change due to clamping)
    was_clamped  : True if the level hit 0 or max_capacity during the update.
                   Useful for detecting prolonged drought or oversupply.

    """

    et_crop_mm: float
    rain_mm: float
    net_change: float
    level_before: float
    level_after: float
    was_clamped: bool


# ── Bucket ─────────────────────────────────────────────────────────────────────


class WaterBucket:
    """
    Soil moisture tracker for a single irrigation zone.

    Parameters
    ----------
    config        : BucketConfig with max_capacity and low_threshold.
    initial_level : Starting bucket level [mm].
                    Defaults to 50 % of max_capacity when not specified —
                    a safe mid-point that avoids triggering immediate watering
                    on first run while not assuming a fully saturated soil.

    """

    def __init__(
        self,
        config: BucketConfig,
        initial_level: float | None = None,
    ) -> None:
        self._config = config
        self._level = (
            initial_level if initial_level is not None else config.max_capacity / 2.0
        )
        # Clamp initial value into valid range
        self._level = self._clamp(self._level)

    # ── Read-only properties ───────────────────────────────────────────────────

    @property
    def level(self) -> float:
        """Current water level in the bucket [mm]."""
        return self._level

    @property
    def config(self) -> BucketConfig:
        return self._config

    @property
    def percentage(self) -> float:
        """Current level as a percentage of max_capacity [0-100]."""
        return round(self._level / self._config.max_capacity * 100.0, 1)

    @property
    def needs_water(self) -> bool:
        """True when the bucket has dropped below the low threshold."""
        return self._level < self._config.low_threshold

    @property
    def deficit_mm(self) -> float:
        """
        Water needed to refill the bucket to max_capacity [mm].

        This is the target irrigation amount: enough to bring the soil back to
        field capacity without over-watering.
        """
        return max(0.0, self._config.max_capacity - self._level)

    # ── State-changing methods ─────────────────────────────────────────────────

    def update(self, rain_mm: float, et0_mm: float, kc: float) -> DailyResult:
        """
        Apply one day's water balance to the bucket.

        Called once per day, typically in the morning before deciding whether
        to irrigate. The order of operations is:
          1. Compute crop consumption = ET₀ x Kc
          2. Compute net change = rain - consumption
          3. Update level and clamp to [0, max_capacity]

        Parameters
        ----------
        rain_mm : Rainfall measured/forecast for the day  [mm]
        et0_mm  : Reference evapotranspiration for the day [mm]
        kc      : Crop coefficient (dimensionless, typically 0.5-1.2)

        Returns
        -------
        DailyResult  — breakdown of the update, used by sensor entities.

        """
        rain_mm = max(0.0, rain_mm)
        et0_mm = max(0.0, et0_mm)
        kc = max(0.0, kc)

        et_crop = et0_mm * kc
        net = rain_mm - et_crop
        before = self._level
        raw_after = before + net

        self._level = self._clamp(raw_after)

        return DailyResult(
            et_crop_mm=round(et_crop, 3),
            rain_mm=round(rain_mm, 3),
            net_change=round(net, 3),
            level_before=round(before, 3),
            level_after=round(self._level, 3),
            was_clamped=(raw_after != self._level),
        )

    def add_irrigation(self, water_mm: float) -> None:
        """
        Add water delivered by the irrigation system.

        Called automatically after a scheduled watering completes, and can
        also be called manually if the user waters by hand.

        Parameters
        ----------
        water_mm : Volume of water delivered, expressed as depth [mm].
                   Conversion: water_mm = volume_litres / zone_area_m²

        """
        self._level = self._clamp(self._level + max(0.0, water_mm))

    def reset(self) -> None:
        """
        Reset the bucket to full capacity.

        Triggered by the HA button entity when the user manually waters the
        garden and wants the system to reflect a fully saturated soil.
        """
        self._level = self._config.max_capacity

    # ── Serialisation (for store.py) ───────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """
        Serialise the mutable state to a plain dict for JSON persistence.

        Only the level is mutable — the config comes from the config entry
        and is not stored here.
        """
        return {"level": round(self._level, 4)}

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        config: BucketConfig,
    ) -> WaterBucket:
        """
        Restore a WaterBucket from a previously serialised dict.

        Parameters
        ----------
        data   : dict as returned by ``to_dict()``
        config : BucketConfig from the current config entry
                 (may differ from when the state was saved if the user
                 changed max_capacity — the clamp in __init__ handles this)

        """
        level = float(data.get("level", config.max_capacity / 2.0))
        return cls(config=config, initial_level=level)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _clamp(self, value: float) -> float:
        return max(0.0, min(self._config.max_capacity, value))

    # ── Dunder ─────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"WaterBucket(level={self._level:.1f} mm, "
            f"{self.percentage:.0f}% of {self._config.max_capacity} mm, "
            f"needs_water={self.needs_water})"
        )
