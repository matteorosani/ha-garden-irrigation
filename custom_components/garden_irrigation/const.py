"""Constants for the Garden Irrigation integration."""

DOMAIN = "garden_irrigation"

# ── Config entry keys ──────────────────────────────────────────────────────────
# These are the exact strings stored in HA's config entry data dict.
# Keeping them in one place avoids typo bugs across config_flow / __init__ / entities.

CONF_ZONE_NAME = "zone_name"
CONF_CROPS = "crops"  # list of crop IDs from crops.json
CONF_PLANTING_DATE = "planting_date"  # ISO date string "YYYY-MM-DD"
CONF_ZONE_AREA = "zone_area"  # m²
CONF_FLOW_RATE = "flow_rate"  # L/min
CONF_MAX_BUCKET = "max_bucket"  # mm — max soil water storage
CONF_LOW_THRESHOLD = "low_threshold"  # mm — water when bucket drops below this
CONF_CALCULATION_TIME = "calculation_time"  # "HH:MM" string

# ── Defaults ───────────────────────────────────────────────────────────────────
DEFAULT_MAX_BUCKET = 25.0  # mm  — reasonable for 30 cm deep vegetable bed
DEFAULT_LOW_THRESHOLD = 12.0  # mm  — 50 % of default max
DEFAULT_CALCULATION_TIME = "23:00"

# ── Units (used in sensor entity definitions) ──────────────────────────────────
UNIT_MM = "mm"
UNIT_MINUTES = "min"
UNIT_PERCENT = "%"
UNIT_MM_DAY = "mm/day"

# ── Storage ────────────────────────────────────────────────────────────────────
STORAGE_KEY = DOMAIN  # filename under .storage/
STORAGE_VERSION = 1
