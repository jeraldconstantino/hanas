"""Test object factories."""

from app.schemas.sensor import ReferenceRange


def build_reference_range() -> ReferenceRange:
    """Build the default lettuce reference range used in tests."""
    return ReferenceRange(
        id=1,
        crop_type="Lettuce",
        growth_stage="Vegetative",
        hydroponic_system_type="DFT",
        ph_target_min=5.5,
        ph_target_max=6.5,
        ec_target_min=1.2,
        ec_target_max=2.0,
        ph_up_dose_ml_per_liter_per_unit=0.80,
        ph_down_dose_ml_per_liter_per_unit=0.71,
        ec_up_dose_ml_per_liter_per_unit=4.45,
        ec_down_dose_ml_per_liter_per_unit=592.12,
        ph_pump_flow_ml_per_min=121,
        ec_pump_flow_ml_per_min=125,
        max_dose_ml_per_cycle=10,
        ph_up_max_dose_ml_per_cycle=10,
        ph_down_max_dose_ml_per_cycle=10,
        ec_up_max_dose_ml_per_cycle=100,
        ec_down_max_dose_ml_per_cycle=2000,
        ph_up_max_duration_ms=10_000,
        ph_down_max_duration_ms=10_000,
        ec_up_max_duration_ms=60_000,
        ec_down_max_duration_ms=960_000,
    )
