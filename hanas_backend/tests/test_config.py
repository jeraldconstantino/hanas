import pytest

from app.core import config
from app.core.config import Settings


def test_app_env_dev_selects_phase_2_profile_over_constructor_values(monkeypatch):
    monkeypatch.delenv("DB_SSLMODE", raising=False)
    monkeypatch.setattr(config, "_env_file_value", lambda option, fallback="": fallback)
    settings = Settings(
        app_env="dev",
        db_schema="prod",
        default_experiment_phase="phase3",
        default_reservoir_max_volume_liters=80.0,
    )

    assert settings.app_env == "dev"
    assert settings.db_schema == "dev"
    assert settings.default_experiment_phase == "phase2"
    assert settings.default_experiment_run_name == "HANAS Phase 2 Baseline Run"
    assert settings.default_reservoir_max_volume_liters == 20.0
    assert settings.minimum_pumpable_reservoir_volume_liters == 5.0
    assert settings.force_fixed_reservoir_volume is True
    assert settings.fixed_reservoir_volume_liters == 20.0
    assert settings.agentic_history_limit == 180
    assert settings.agentic_control_history_limit == 1440
    assert settings.agentic_history_freshness_gap_seconds == 900
    assert settings.agentic_adaptive_history_window_seconds == 86400
    assert settings.agentic_ai_llm_timeout_seconds == 15.0
    assert settings.batch_analysis_interval_seconds == 600
    assert settings.batch_analysis_window_readings == 15
    assert settings.batch_pending_command_expiry_seconds == 900
    assert settings.app_timezone == "Asia/Manila"
    assert settings.db_sslmode == ""


def test_app_env_prod_selects_phase_3_profile_over_constructor_values(monkeypatch):
    monkeypatch.delenv("DB_SSLMODE", raising=False)
    monkeypatch.setattr(config, "_env_file_value", lambda option, fallback="": fallback)
    settings = Settings(
        app_env="prod",
        db_schema="dev",
        default_experiment_phase="phase2",
        default_reservoir_max_volume_liters=20.0,
    )

    assert settings.app_env == "prod"
    assert settings.db_schema == "prod"
    assert settings.default_experiment_phase == "phase3"
    assert settings.default_experiment_run_name == "HANAS Phase 3 Live Run"
    assert settings.default_reservoir_max_volume_liters == 70.0
    assert settings.minimum_pumpable_reservoir_volume_liters == 20.0
    assert settings.force_fixed_reservoir_volume is False
    assert settings.fixed_reservoir_volume_liters == 70.0
    assert settings.default_mixing_time_seconds == 300
    assert settings.agentic_base_mixing_time_seconds == 180
    assert settings.agentic_history_limit == 180
    assert settings.agentic_history_freshness_gap_seconds == 900
    assert settings.agentic_control_history_limit == 1440
    assert settings.agentic_adaptive_history_window_seconds == 86400
    assert settings.agentic_ai_llm_timeout_seconds == 15.0
    assert settings.batch_analysis_interval_seconds == 600
    assert settings.batch_analysis_window_readings == 15
    assert settings.batch_pending_command_expiry_seconds == 900
    assert settings.app_timezone == "Asia/Manila"
    assert settings.db_sslmode == ""


def test_app_env_dev_phase3_selects_dev_database_with_batch_profile(monkeypatch):
    monkeypatch.delenv("DB_SSLMODE", raising=False)
    monkeypatch.setattr(config, "_env_file_value", lambda option, fallback="": fallback)
    settings = Settings(app_env="dev_phase3")

    assert settings.app_env == "dev_phase3"
    assert settings.db_schema == "dev"
    assert settings.default_experiment_phase == "phase3"
    assert settings.default_experiment_run_name == "HANAS Phase 3 Dev Batch Run"
    assert settings.default_control_strategy == "agentic_ai"
    assert settings.default_reservoir_max_volume_liters == 20.0
    assert settings.minimum_pumpable_reservoir_volume_liters == 5.0
    assert settings.force_fixed_reservoir_volume is True
    assert settings.fixed_reservoir_volume_liters == 20.0
    assert settings.default_mixing_time_seconds == 300
    assert settings.agentic_base_mixing_time_seconds == 180
    assert settings.batch_analysis_enabled is True
    assert settings.batch_analysis_interval_seconds == 600
    assert settings.batch_analysis_window_readings == 15
    assert settings.batch_pending_command_expiry_seconds == 900


def test_reservoir_minimum_must_be_below_capacity(monkeypatch):
    """Invalid operating-volume configuration must fail during startup."""
    original_config_float = config._config_float

    def invalid_reservoir_config(section, option, fallback, app_env=None):
        if option == "default_reservoir_max_volume_liters":
            return 20.0
        if option == "minimum_pumpable_reservoir_volume_liters":
            return 20.0
        return original_config_float(section, option, fallback, app_env)

    monkeypatch.setattr(config, "_config_float", invalid_reservoir_config)

    with pytest.raises(ValueError, match="must be non-negative and below"):
        Settings(app_env="dev")


def test_db_sslmode_env_override_wins_over_selected_profile(monkeypatch):
    monkeypatch.setenv("DB_SSLMODE", "require")

    settings = Settings(app_env="dev")

    assert settings.db_schema == "dev"
    assert settings.db_sslmode == "require"


def test_app_timezone_comes_from_config_profile(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "UTC")
    monkeypatch.setattr(config, "_env_file_value", lambda option, fallback="": fallback)

    settings = Settings(app_env="prod")

    assert settings.app_timezone == "Asia/Manila"
