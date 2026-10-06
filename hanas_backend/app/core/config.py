"""Application and database configuration helpers."""

import os
from configparser import ConfigParser
from pathlib import Path
from typing import TypedDict

from pydantic_settings import BaseSettings, SettingsConfigDict

from app import APP_DIR, APP_VERSION, PROJECT_DIR

DEFAULT_APP_CONFIG = APP_DIR / "core" / "config.ini"
DEFAULT_DATABASE_CONFIG = APP_DIR / "database" / "database.ini"


def _read_default_config(filename: Path = DEFAULT_APP_CONFIG) -> ConfigParser:
    """Read checked-in non-secret application defaults."""
    parser = ConfigParser()
    parser.read(filename)
    return parser


_DEFAULT_CONFIG = _read_default_config()


def _env_file_value(option: str, fallback: str = "") -> str:
    """Read a simple KEY=value from the local .env file before Settings exists."""
    env_path = PROJECT_DIR / ".env"
    if not env_path.exists():
        return fallback

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        if key.strip() == option:
            return value.strip().strip('"').strip("'")

    return fallback


def _selected_app_env() -> str:
    """Return the selected non-secret config profile."""
    app_env = os.getenv("APP_ENV") or _env_file_value("APP_ENV", "prod")
    return app_env.strip().lower() or "prod"


APP_ENV = _selected_app_env()


def _profile_section(section: str, app_env: str | None = None) -> str:
    return f"{section}.{app_env or APP_ENV}"


def _config_value(section: str, option: str, fallback: str, app_env: str | None = None) -> str:
    """Return a string default from config.ini."""
    profile_section = _profile_section(section, app_env)
    if _DEFAULT_CONFIG.has_option(profile_section, option):
        return _DEFAULT_CONFIG.get(profile_section, option)
    return _DEFAULT_CONFIG.get(section, option, fallback=fallback)


def _config_int(section: str, option: str, fallback: int, app_env: str | None = None) -> int:
    """Return an integer default from config.ini."""
    profile_section = _profile_section(section, app_env)
    if _DEFAULT_CONFIG.has_option(profile_section, option):
        return _DEFAULT_CONFIG.getint(profile_section, option)
    return _DEFAULT_CONFIG.getint(section, option, fallback=fallback)


def _config_float(section: str, option: str, fallback: float, app_env: str | None = None) -> float:
    """Return a float default from config.ini."""
    profile_section = _profile_section(section, app_env)
    if _DEFAULT_CONFIG.has_option(profile_section, option):
        return _DEFAULT_CONFIG.getfloat(profile_section, option)
    return _DEFAULT_CONFIG.getfloat(section, option, fallback=fallback)


def _config_bool(section: str, option: str, fallback: bool, app_env: str | None = None) -> bool:
    """Return a boolean default from config.ini."""
    profile_section = _profile_section(section, app_env)
    if _DEFAULT_CONFIG.has_option(profile_section, option):
        return _DEFAULT_CONFIG.getboolean(profile_section, option)
    return _DEFAULT_CONFIG.getboolean(section, option, fallback=fallback)


class Settings(BaseSettings):
    """Application settings with config.ini defaults and environment overrides."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_username: str = ""
    db_password: str = ""
    db_name: str = ""
    db_host: str = ""
    db_port: str = ""
    app_env: str = APP_ENV
    db_schema: str = _config_value("database", "schema", "prod")
    db_sslmode: str = ""
    database_config_file: Path = DEFAULT_DATABASE_CONFIG
    database_config_section: str = "postgresql"
    device_api_token: str = ""
    operator_api_token: str = ""
    cors_allow_origins: str = _config_value(
        "app",
        "cors_allow_origins",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174",
    )
    app_name: str = _config_value("app", "name", "HANAS")
    app_version: str = _config_value("app", "version", APP_VERSION)
    build_label: str = ""
    app_timezone: str = _config_value("app", "timezone", "Asia/Manila")
    app_full_name: str = _config_value(
        "app",
        "full_name",
        "Hydroponic Agentic Nutrient Adjustment System",
    )
    default_reference_range_id: int = _config_int("app", "default_reference_range_id", 1)
    default_experiment_run_name: str = _config_value(
        "app",
        "default_experiment_run_name",
        "HANAS Phase 3 Live Run",
    )
    default_experiment_phase: str = _config_value("app", "default_experiment_phase", "phase3")
    default_control_strategy: str = _config_value("app", "default_control_strategy", "agentic_ai")
    default_crop_type: str = _config_value("app", "default_crop_type", "Lettuce")
    default_hydroponic_system_type: str = _config_value(
        "app",
        "default_hydroponic_system_type",
        "DFT",
    )
    default_crop_harvest_start_day: int = _config_int(
        "app",
        "default_crop_harvest_start_day",
        30,
    )
    default_crop_harvest_end_day: int = _config_int(
        "app",
        "default_crop_harvest_end_day",
        35,
    )
    default_reservoir_max_volume_liters: float = _config_float(
        "app",
        "default_reservoir_max_volume_liters",
        70.0,
    )
    minimum_pumpable_reservoir_volume_liters: float = _config_float(
        "app",
        "minimum_pumpable_reservoir_volume_liters",
        20.0,
    )
    force_fixed_reservoir_volume: bool = _config_bool(
        "app",
        "force_fixed_reservoir_volume",
        False,
    )
    fixed_reservoir_volume_liters: float = _config_float(
        "app",
        "fixed_reservoir_volume_liters",
        70.0,
    )
    default_sampling_interval_seconds: int = _config_int(
        "app",
        "default_sampling_interval_seconds",
        60,
    )
    default_mixing_time_seconds: int = _config_int("app", "default_mixing_time_seconds", 300)
    agentic_base_mixing_time_seconds: int = _config_int(
        "app",
        "agentic_base_mixing_time_seconds",
        180,
    )
    default_initial_confirmation_gap_seconds: int = _config_int(
        "app",
        "default_initial_confirmation_gap_seconds",
        30,
    )
    default_stability_required_seconds: int = _config_int(
        "app",
        "default_stability_required_seconds",
        30,
    )
    default_ph_stability_threshold: float = _config_float(
        "app",
        "default_ph_stability_threshold",
        0.03,
    )
    default_ec_stability_threshold: float = _config_float(
        "app",
        "default_ec_stability_threshold",
        0.03,
    )
    agentic_history_limit: int = _config_int("app", "agentic_history_limit", 180)
    agentic_control_history_limit: int = _config_int(
        "app",
        "agentic_control_history_limit",
        1440,
    )
    agentic_history_freshness_gap_seconds: int = _config_int(
        "app",
        "agentic_history_freshness_gap_seconds",
        900,
    )
    agentic_adaptive_history_window_seconds: int = _config_int(
        "app",
        "agentic_adaptive_history_window_seconds",
        10_800,
    )
    agentic_ai_llm_timeout_seconds: float = _config_float(
        "app",
        "agentic_ai_llm_timeout_seconds",
        10.0,
    )
    agentic_ai_use_llm: bool = False
    full_agentic_mode_enabled: bool = _config_bool(
        "app",
        "full_agentic_mode_enabled",
        False,
    )
    agentic_ai_model: str = _config_value("app", "agentic_ai_model", "gpt-4.1-nano")
    agentic_ai_monitoring_model: str = _config_value("app", "agentic_ai_monitoring_model", "gpt-4.1-mini")
    agentic_ai_diagnostic_model: str = _config_value("app", "agentic_ai_diagnostic_model", "gpt-4.1-mini")
    agentic_ai_dose_planning_model: str = _config_value("app", "agentic_ai_dose_planning_model", "gpt-4.1-mini")
    agentic_ai_fallback_model: str = _config_value(
        "app",
        "agentic_ai_fallback_model",
        "gpt-4.1-mini",
    )
    batch_analysis_enabled: bool = _config_bool("app", "batch_analysis_enabled", False)
    batch_analysis_interval_seconds: int = _config_int(
        "app",
        "batch_analysis_interval_seconds",
        600,
    )
    batch_analysis_window_readings: int = _config_int(
        "app",
        "batch_analysis_window_readings",
        30,
    )
    batch_pending_command_expiry_seconds: int = _config_int(
        "app",
        "batch_pending_command_expiry_seconds",
        900,
    )
    overview_summary_enabled: bool = _config_bool("app", "overview_summary_enabled", False)
    overview_summary_interval_seconds: int = _config_int(
        "app",
        "overview_summary_interval_seconds",
        43_200,
    )
    overview_summary_max_readings: int = _config_int(
        "app",
        "overview_summary_max_readings",
        1440,
    )
    ph_emergency_buffer_below: float = _config_float("app", "ph_emergency_buffer_below", 0.5)
    ph_emergency_buffer_above: float = _config_float("app", "ph_emergency_buffer_above", 1.0)
    ec_emergency_buffer_below: float = _config_float("app", "ec_emergency_buffer_below", 0.4)
    ec_emergency_buffer_above: float = _config_float("app", "ec_emergency_buffer_above", 0.5)
    openai_api_key: str = ""
    sms_notifications_enabled: bool = False
    sms_alert_cooldown_seconds: int = 900
    sms_sender_id: str = ""
    sms_receiver_phone_number: str = ""
    semaphore_api_key: str = ""
    sms_status_poll_interval_seconds: int = 60
    sms_status_poll_batch_size: int = 20
    # Keep retrying for up to roughly three hours at the default one-minute
    # interval. Provider delivery states can lag behind actual handset receipt.
    sms_status_max_checks: int = 180
    sms_status_lookback_days: int = 30
    human_in_the_loop_enabled: bool = False
    minimum_pump_duration_ms: int = _config_int("dosing", "minimum_pump_duration_ms", 500)
    maximum_pump_duration_ms: int = _config_int("dosing", "maximum_pump_duration_ms", 10_000)
    minimum_reliable_pump_duration_ms: int = _config_int(
        "dosing",
        "minimum_reliable_pump_duration_ms",
        1500,
    )

    def model_post_init(self, __context: object) -> None:
        """Apply non-secret config.ini values from the selected APP_ENV profile."""
        app_env = self.app_env.strip().lower() or "prod"
        env_db_sslmode = os.getenv("DB_SSLMODE") or _env_file_value("DB_SSLMODE")

        self.app_env = app_env
        self.db_schema = _config_value("database", "schema", "prod", app_env)
        self.db_sslmode = env_db_sslmode
        self.app_name = _config_value("app", "name", "HANAS", app_env)
        self.app_timezone = _config_value("app", "timezone", "Asia/Manila", app_env)
        self.app_full_name = _config_value(
            "app",
            "full_name",
            "Hydroponic Agentic Nutrient Adjustment System",
            app_env,
        )
        if not os.getenv("CORS_ALLOW_ORIGINS") and not _env_file_value("CORS_ALLOW_ORIGINS"):
            self.cors_allow_origins = _config_value(
                "app",
                "cors_allow_origins",
                self.cors_allow_origins,
                app_env,
            )
        self.default_reference_range_id = _config_int(
            "app",
            "default_reference_range_id",
            1,
            app_env,
        )
        self.default_experiment_run_name = _config_value(
            "app",
            "default_experiment_run_name",
            "HANAS Phase 3 Live Run",
            app_env,
        )
        self.default_experiment_phase = _config_value(
            "app",
            "default_experiment_phase",
            "phase3",
            app_env,
        )
        self.default_control_strategy = _config_value(
            "app",
            "default_control_strategy",
            "baseline",
            app_env,
        )
        self.default_crop_type = _config_value(
            "app",
            "default_crop_type",
            "Lettuce",
            app_env,
        )
        self.default_hydroponic_system_type = _config_value(
            "app",
            "default_hydroponic_system_type",
            "DFT",
            app_env,
        )
        self.default_crop_harvest_start_day = _config_int(
            "app",
            "default_crop_harvest_start_day",
            30,
            app_env,
        )
        self.default_crop_harvest_end_day = _config_int(
            "app",
            "default_crop_harvest_end_day",
            35,
            app_env,
        )
        self.default_reservoir_max_volume_liters = _config_float(
            "app",
            "default_reservoir_max_volume_liters",
            70.0,
            app_env,
        )
        self.minimum_pumpable_reservoir_volume_liters = _config_float(
            "app",
            "minimum_pumpable_reservoir_volume_liters",
            20.0,
            app_env,
        )
        self.force_fixed_reservoir_volume = _config_bool(
            "app",
            "force_fixed_reservoir_volume",
            False,
            app_env,
        )
        self.fixed_reservoir_volume_liters = _config_float(
            "app",
            "fixed_reservoir_volume_liters",
            self.default_reservoir_max_volume_liters,
            app_env,
        )
        self.default_sampling_interval_seconds = _config_int(
            "app",
            "default_sampling_interval_seconds",
            60,
            app_env,
        )
        self.default_mixing_time_seconds = _config_int(
            "app",
            "default_mixing_time_seconds",
            300,
            app_env,
        )
        self.agentic_base_mixing_time_seconds = _config_int(
            "app",
            "agentic_base_mixing_time_seconds",
            180,
            app_env,
        )
        self.default_initial_confirmation_gap_seconds = _config_int(
            "app",
            "default_initial_confirmation_gap_seconds",
            30,
            app_env,
        )
        self.default_stability_required_seconds = _config_int(
            "app",
            "default_stability_required_seconds",
            30,
            app_env,
        )
        self.default_ph_stability_threshold = _config_float(
            "app",
            "default_ph_stability_threshold",
            0.03,
            app_env,
        )
        self.default_ec_stability_threshold = _config_float(
            "app",
            "default_ec_stability_threshold",
            0.03,
            app_env,
        )
        self.agentic_history_limit = _config_int(
            "app",
            "agentic_history_limit",
            180,
            app_env,
        )
        self.agentic_control_history_limit = _config_int(
            "app",
            "agentic_control_history_limit",
            1440,
            app_env,
        )
        self.agentic_history_freshness_gap_seconds = _config_int(
            "app",
            "agentic_history_freshness_gap_seconds",
            900,
            app_env,
        )
        self.agentic_adaptive_history_window_seconds = _config_int(
            "app",
            "agentic_adaptive_history_window_seconds",
            10_800,
            app_env,
        )
        self.agentic_ai_llm_timeout_seconds = _config_float(
            "app",
            "agentic_ai_llm_timeout_seconds",
            10.0,
            app_env,
        )
        self.agentic_ai_model = _config_value(
            "app",
            "agentic_ai_model",
            self.agentic_ai_model,
            app_env,
        )
        self.agentic_ai_fallback_model = _config_value(
            "app",
            "agentic_ai_fallback_model",
            self.agentic_ai_fallback_model,
            app_env,
        )
        self.agentic_ai_monitoring_model = _config_value(
            "app", "agentic_ai_monitoring_model", self.agentic_ai_monitoring_model, app_env,
        )
        self.agentic_ai_diagnostic_model = _config_value(
            "app", "agentic_ai_diagnostic_model", self.agentic_ai_diagnostic_model, app_env,
        )
        self.agentic_ai_dose_planning_model = _config_value(
            "app", "agentic_ai_dose_planning_model", self.agentic_ai_dose_planning_model, app_env,
        )
        self.batch_analysis_enabled = _config_bool(
            "app",
            "batch_analysis_enabled",
            self.batch_analysis_enabled,
            app_env,
        )
        self.batch_analysis_interval_seconds = _config_int(
            "app",
            "batch_analysis_interval_seconds",
            self.batch_analysis_interval_seconds,
            app_env,
        )
        self.batch_analysis_window_readings = _config_int(
            "app",
            "batch_analysis_window_readings",
            self.batch_analysis_window_readings,
            app_env,
        )
        self.batch_pending_command_expiry_seconds = _config_int(
            "app",
            "batch_pending_command_expiry_seconds",
            self.batch_pending_command_expiry_seconds,
            app_env,
        )
        self.overview_summary_enabled = _config_bool(
            "app",
            "overview_summary_enabled",
            self.overview_summary_enabled,
            app_env,
        )
        self.overview_summary_interval_seconds = _config_int(
            "app",
            "overview_summary_interval_seconds",
            self.overview_summary_interval_seconds,
            app_env,
        )
        self.overview_summary_max_readings = _config_int(
            "app",
            "overview_summary_max_readings",
            self.overview_summary_max_readings,
            app_env,
        )
        self.ph_emergency_buffer_below = _config_float(
            "app", "ph_emergency_buffer_below", self.ph_emergency_buffer_below, app_env,
        )
        self.ph_emergency_buffer_above = _config_float(
            "app", "ph_emergency_buffer_above", self.ph_emergency_buffer_above, app_env,
        )
        self.ec_emergency_buffer_below = _config_float(
            "app", "ec_emergency_buffer_below", self.ec_emergency_buffer_below, app_env,
        )
        self.ec_emergency_buffer_above = _config_float(
            "app", "ec_emergency_buffer_above", self.ec_emergency_buffer_above, app_env,
        )
        self.minimum_pump_duration_ms = _config_int(
            "dosing",
            "minimum_pump_duration_ms",
            500,
            app_env,
        )
        self.maximum_pump_duration_ms = _config_int(
            "dosing",
            "maximum_pump_duration_ms",
            10_000,
            app_env,
        )
        self.minimum_reliable_pump_duration_ms = _config_int(
            "dosing",
            "minimum_reliable_pump_duration_ms",
            1500,
            app_env,
        )
        if not (
            0 <= self.minimum_pumpable_reservoir_volume_liters
            < self.default_reservoir_max_volume_liters
        ):
            raise ValueError(
                "minimum_pumpable_reservoir_volume_liters must be non-negative "
                "and below default_reservoir_max_volume_liters"
            )

    @property
    def allowed_cors_origins(self) -> list[str]:
        """Return configured browser origins allowed to call the API."""
        return [
            origin.strip()
            for origin in self.cors_allow_origins.split(",")
            if origin.strip()
        ]


class DatabaseConfig(TypedDict):
    """Connection settings expected by psycopg2."""

    dbname: str | None
    host: str | None
    port: str | None
    user: str
    password: str
    sslmode: str | None


settings = Settings()


def load_db_config(
    filename: str | Path | None = None,
    section: str | None = None,
    app_settings: Settings | None = None,
) -> DatabaseConfig:
    """Load PostgreSQL connection settings from environment with INI fallback."""
    current_settings = app_settings or settings
    config_path = Path(filename or current_settings.database_config_file)
    config_section = section or current_settings.database_config_section

    if not config_path.is_absolute():
        config_path = DEFAULT_DATABASE_CONFIG.parent / config_path

    if current_settings.db_host or current_settings.db_name:
        return {
            "dbname": current_settings.db_name,
            "host": current_settings.db_host,
            "port": current_settings.db_port or "5432",
            "user": current_settings.db_username,
            "password": current_settings.db_password,
            "sslmode": current_settings.db_sslmode or None,
        }

    parser = ConfigParser()
    parser.read(config_path)
    if not parser.has_section(config_section):
        msg = f"Section '{config_section}' not found in {config_path}"
        raise ValueError(msg)

    config = parser[config_section]
    return {
        "dbname": config.get("dbname") or config.get("database"),
        "host": config.get("host"),
        "port": config.get("port"),
        "user": current_settings.db_username,
        "password": current_settings.db_password,
        "sslmode": current_settings.db_sslmode or config.get("sslmode"),
    }
