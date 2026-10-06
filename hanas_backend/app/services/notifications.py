"""Optional operator notifications for HANAS alerts."""

from __future__ import annotations

import json
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from loguru import logger

from app.core.config import settings
from app.schemas.sensor import DosingDecision, SensorPayload
from app.services.runtime_settings import runtime_bool_setting


_last_sent_at_by_key: dict[str, float] = {}
SMS_SINGLE_SEGMENT_CHARS = 160


@dataclass(frozen=True)
class SmsAlert:
    """SMS alert payload prepared from a control decision."""

    alert_type: str
    key: str
    body: str


class NotificationLogWriter(Protocol):
    """Persistence contract for SMS notification attempts."""

    def create_notification_log(
        self,
        *,
        provider: str,
        recipient_number: str,
        sender_id: str | None,
        alert_type: str,
        message_body: str,
        status: str,
        system_log_id: int,
        control_cycle_id: int,
        provider_response: str | None = None,
        provider_message_id: int | None = None,
        provider_status_updated_at: datetime | None = None,
        error_message: str | None = None,
    ) -> None:
        """Persist one SMS notification attempt."""

    def get_system_setting(self, key: str) -> object:
        """Return a runtime setting value from the DB, or None if not set."""


def maybe_send_decision_alert(
    payload: SensorPayload,
    decision: DosingDecision,
    *,
    log_id: int,
    control_cycle_id: int,
    notification_log_writer: NotificationLogWriter | None = None,
) -> bool:
    """Send an SMS alert for important decision risk flags when configured."""
    if runtime_bool_setting(
        notification_log_writer,
        "monitoring_mode_enabled",
        default=False,
    ):
        logger.info("SMS alert suppressed because Monitoring Only is active.")
        return False

    alert = _alert_for_decision(
        payload,
        decision,
        log_id=log_id,
        control_cycle_id=control_cycle_id,
    )
    if alert is None:
        return False

    if not _sms_is_configured(notification_log_writer):
        logger.debug("SMS alert suppressed because SMS notifications are not configured.")
        _log_notification_attempt(
            notification_log_writer,
            alert,
            log_id=log_id,
            control_cycle_id=control_cycle_id,
            status="disabled",
        )
        return False

    if _is_in_cooldown(alert.key):
        logger.info("SMS alert suppressed by cooldown: {}", alert.key)
        _log_notification_attempt(
            notification_log_writer,
            alert,
            log_id=log_id,
            control_cycle_id=control_cycle_id,
            status="suppressed",
            provider_response="Suppressed by SMS alert cooldown.",
        )
        return False

    try:
        provider_response = _send_sms(alert.body)
    except HTTPError as exc:
        error_body = _read_http_error_body(exc)
        logger.warning("SMS alert failed: {} {}", exc, error_body)
        _log_notification_attempt(
            notification_log_writer,
            alert,
            log_id=log_id,
            control_cycle_id=control_cycle_id,
            status="failed",
            error_message=f"{exc} {error_body}".strip(),
        )
        return False
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("SMS alert failed: {}", exc)
        _log_notification_attempt(
            notification_log_writer,
            alert,
            log_id=log_id,
            control_cycle_id=control_cycle_id,
            status="failed",
            error_message=str(exc),
        )
        return False

    provider_message_id, provider_status = _parse_semaphore_message(provider_response)
    recorded_status = provider_status or "submitted"
    _last_sent_at_by_key[alert.key] = time.monotonic()
    logger.info(
        "SMS alert submitted: {} provider_status={} provider_message_id={}",
        alert.key,
        recorded_status,
        provider_message_id,
    )
    _log_notification_attempt(
        notification_log_writer,
        alert,
        log_id=log_id,
        control_cycle_id=control_cycle_id,
        status=recorded_status,
        provider_response=provider_response,
        provider_message_id=provider_message_id,
        provider_status_updated_at=datetime.now(timezone.utc),
    )
    return True


def _alert_for_decision(
    payload: SensorPayload,
    decision: DosingDecision,
    *,
    log_id: int,
    control_cycle_id: int,
) -> SmsAlert | None:
    """Build an alert for decision states that need operator attention."""
    risk_flags = set(_string_list(decision.metadata.get("risk_flags")))
    reason = decision.reason
    alert_type: str | None = None

    if decision.decision == "sensor_anomaly" or "sensor_anomaly" in risk_flags:
        alert_type = "sensor_anomaly"
        reason = "Sensor anomaly detected. Verify probes and reservoir readings."
    elif "possible_delivery_issue" in risk_flags or _delivery_issue_detected(decision):
        alert_type = "possible_delivery_issue"
        reason = "Possible pump delivery issue. Check solution container, tubing, and pump."
    elif _is_dosing_decision(decision):
        alert_type = "dosing_started"
    elif decision.decision == "wait_human_review" or "human_review_required" in risk_flags:
        alert_type = "human_review_required"
        reason = "Human review required before agentic AI pump activation."
    elif decision.decision in {"wait_safety_gate", "error"}:
        alert_type = decision.decision
        reason = decision.reason

    if alert_type is None:
        return None

    body = _sms_body(
        alert_type,
        payload,
        decision,
        log_id=log_id,
        control_cycle_id=control_cycle_id,
        reason=reason,
    )
    key = f"{alert_type}:{decision.pump_activated}:{decision.decision}"
    return SmsAlert(alert_type=alert_type, key=key, body=body)


def _sms_is_configured(notification_log_writer: NotificationLogWriter | None = None) -> bool:
    """Return whether SMS notifications can be sent."""
    sms_enabled = runtime_bool_setting(
        notification_log_writer,
        "sms_enabled",
        default=settings.sms_notifications_enabled,
    )
    return all(
        [
            sms_enabled,
            settings.semaphore_api_key,
            settings.sms_receiver_phone_number,
        ]
    )


def _is_in_cooldown(alert_key: str) -> bool:
    """Return whether an alert key was recently sent."""
    cooldown_seconds = max(settings.sms_alert_cooldown_seconds, 0)
    last_sent_at = _last_sent_at_by_key.get(alert_key)
    if last_sent_at is None:
        return False
    return time.monotonic() - last_sent_at < cooldown_seconds


def _send_sms(body: str) -> str:
    """Send a Semaphore alert to a Philippine mobile number."""
    form_payload = {
        "apikey": settings.semaphore_api_key,
        "number": _semaphore_recipient(settings.sms_receiver_phone_number),
        "message": body,
    }
    if settings.sms_sender_id:
        form_payload["sendername"] = settings.sms_sender_id

    request = Request(
        "https://api.semaphore.co/api/v4/messages",
        data=urlencode(form_payload).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8", errors="replace").strip()


def _log_notification_attempt(
    writer: NotificationLogWriter | None,
    alert: SmsAlert,
    *,
    log_id: int,
    control_cycle_id: int,
    status: str,
    provider_response: str | None = None,
    provider_message_id: int | None = None,
    provider_status_updated_at: datetime | None = None,
    error_message: str | None = None,
) -> None:
    """Persist an SMS notification attempt without blocking control flow."""
    if writer is None:
        return

    try:
        writer.create_notification_log(
            provider="semaphore",
            recipient_number=settings.sms_receiver_phone_number,
            sender_id=settings.sms_sender_id or None,
            alert_type=alert.alert_type,
            message_body=alert.body,
            status=status,
            system_log_id=log_id,
            control_cycle_id=control_cycle_id,
            provider_response=provider_response,
            provider_message_id=provider_message_id,
            provider_status_updated_at=provider_status_updated_at,
            error_message=error_message,
        )
    except Exception as exc:  # pragma: no cover - defensive audit logging guard
        logger.warning("Failed to persist SMS notification log: {}", exc)


def _parse_semaphore_message(response_body: str | None) -> tuple[int | None, str | None]:
    """Extract the provider message ID and normalized status from a Semaphore response."""
    if not response_body:
        return None, None
    try:
        payload = json.loads(response_body)
    except (json.JSONDecodeError, TypeError):
        return None, None

    record = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(record, dict):
        return None, None

    raw_message_id = record.get("message_id")
    try:
        message_id = int(raw_message_id) if raw_message_id is not None else None
    except (TypeError, ValueError):
        message_id = None

    raw_status = record.get("status")
    status = str(raw_status).strip().lower() if raw_status is not None else None
    return message_id, status or None


def _semaphore_recipient(phone_number: str) -> str:
    """Return Semaphore recipient format, e.g. +639... -> 09..."""
    normalized = phone_number.strip().replace(" ", "").replace("-", "")
    if normalized.startswith("+63"):
        return f"0{normalized[3:]}"
    if normalized.startswith("63"):
        return f"0{normalized[2:]}"
    return normalized


def _read_http_error_body(exc: HTTPError) -> str:
    """Return a compact provider error body for debugging failed SMS sends."""
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not body:
        return ""
    return body[:1000]


def _sms_body(
    alert_type: str,
    payload: SensorPayload,
    decision: DosingDecision,
    *,
    log_id: int,
    control_cycle_id: int,
    reason: str,
) -> str:
    """Return a human-friendly operator SMS within Semaphore segment limits."""
    readings = _readings_text(payload)

    if alert_type == "dosing_started":
        mixing_seconds = _mixing_seconds(decision)
        severity = _sms_severity(decision)
        if not decision.ph_within_range and not decision.ec_within_range:
            follow_up = (
                f"Mix {mixing_seconds} s. {_operator_check_action(decision)}"
                if mixing_seconds > 0
                else _operator_check_action(decision)
            )
            body = (
                f"HANAS: Both outside target: pH {payload.ph:.2f}, "
                f"EC {payload.ec:.2f} mS/cm. I scheduled {_dose_command_text(decision)}. "
                f"Water temperature {payload.temperature:.1f} C. {follow_up}"
            )
            return _fit_semaphore_sms(body)

        if mixing_seconds > 0:
            follow_up = (
                f"Mixing: {mixing_seconds} s. {_operator_check_action(decision)}"
                if severity == "check_system"
                else f"Mix {mixing_seconds} s. Then I will recheck."
            )
        else:
            follow_up = (
                _operator_check_action(decision)
                if severity == "check_system"
                else "I'll check the next reading."
            )
        body = (
            f"HANAS: I scheduled {_dose_command_text(decision)} for "
            f"{_dose_condition_text(payload, decision)}. "
            f"{_companion_reading_text(payload, decision, include_reservoir=severity != 'check_system')} "
            f"{follow_up}"
        )
        return _fit_semaphore_sms(body)

    if alert_type == "possible_delivery_issue":
        delivery_context = _delivery_issue_command_context(decision)
        body = (
            f"HANAS: Possible pump delivery issue detected{delivery_context}. "
            f"{readings} Please inspect the solution line and pump."
        )
    elif alert_type == "sensor_anomaly":
        body = (
            f"HANAS: Unusual sensor readings detected. {readings} "
            "Please verify the probes and reservoir before dosing."
        )
    elif alert_type == "human_review_required":
        body = (
            f"HANAS: I need your review before I activate a pump. {readings} "
            f"Please review cycle #{control_cycle_id} in the dashboard."
        )
    else:
        body = (
            f"HANAS: I need your attention: {_alert_title(alert_type)}. {readings} "
            f"Please review cycle #{control_cycle_id} in the dashboard."
        )
    return _fit_semaphore_sms(body)


def _fit_semaphore_sms(body: str) -> str:
    """Keep every ASCII alert within one 160-character Semaphore segment."""
    clean_body = _ascii_sms_text(body)
    if len(clean_body) > SMS_SINGLE_SEGMENT_CHARS:
        clean_body = _truncate_sms_text(clean_body, SMS_SINGLE_SEGMENT_CHARS)
    return clean_body


def _ascii_sms_text(value: str) -> str:
    """Normalize model or controller text to plain ASCII for predictable SMS billing."""
    plain_sentences = value.replace(";", ".").replace("—", ".").replace("–", "-")
    normalized = unicodedata.normalize("NFKD", " ".join(plain_sentences.split()))
    return normalized.encode("ascii", errors="ignore").decode("ascii")


def _truncate_sms_text(value: str, limit: int) -> str:
    """Trim at a word boundary while preserving the configured character ceiling."""
    if len(value) <= limit:
        return value
    if limit <= 3:
        return "." * max(limit, 0)
    shortened = value[: limit - 3].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{shortened}..."


def _is_dosing_decision(decision: DosingDecision) -> bool:
    """Return whether the decision commands a physical pump action."""
    return (
        decision.pump_activated not in {"none", ""}
        and decision.dose_ml > 0
        and decision.duration_ms > 0
    )


def _mixing_seconds(decision: DosingDecision) -> int:
    """Return the effective post-dose mixing seconds from decision metadata."""
    mixing_window = decision.metadata.get("mixing_window")
    if isinstance(mixing_window, dict):
        effective_seconds = mixing_window.get("effective_seconds")
        if isinstance(effective_seconds, int | float) and effective_seconds > 0:
            return round(effective_seconds)
    return 0


def _readings_text(payload: SensorPayload) -> str:
    """Return current readings as a short, natural sentence."""
    return (
        f"pH {payload.ph:.2f}, EC {payload.ec:.2f} mS/cm, "
        f"water temperature {payload.temperature:.1f} C."
    )


def _dose_condition_text(payload: SensorPayload, decision: DosingDecision) -> str:
    """Explain the affected measurement in conversational language."""
    conditions = {
        "ph_low": f"low pH at {payload.ph:.2f}",
        "ph_high": f"high pH at {payload.ph:.2f}",
        "ec_low": f"low EC at {payload.ec:.2f} mS/cm",
        "ec_high": f"high EC at {payload.ec:.2f} mS/cm",
    }
    return conditions.get(decision.decision, "a reading was outside its target")


def _dose_command_text(decision: DosingDecision) -> str:
    """Describe the commanded volume accurately, including both EC-up components."""
    if decision.pump_activated == "ec_up":
        return f"{decision.dose_ml:.2f} mL each of EC Up A/B"
    return f"{decision.dose_ml:.2f} mL {_pump_label(decision.pump_activated)}"


def _delivery_issue_command_context(decision: DosingDecision) -> str:
    """Describe the prior dose without exposing zero-volume or missing pump values."""
    delivery_issue = decision.metadata.get("delivery_issue")
    issue_metadata = delivery_issue if isinstance(delivery_issue, dict) else {}

    metadata_dose = issue_metadata.get("latest_same_pump_dose_ml")
    dose_ml = (
        float(metadata_dose)
        if isinstance(metadata_dose, int | float)
        and not isinstance(metadata_dose, bool)
        and metadata_dose > 0
        else decision.dose_ml if decision.dose_ml > 0 else None
    )

    metadata_pump = issue_metadata.get("current_pump")
    pump = metadata_pump if isinstance(metadata_pump, str) else decision.pump_activated
    if pump.strip().lower() in {"", "none"}:
        pump = decision.pump_activated
    if pump.strip().lower() in {"", "none"}:
        prior_decision = issue_metadata.get("latest_same_pump_decision")
        pump = {
            "ph_low": "ph_up",
            "ph_high": "ph_down",
            "ec_low": "ec_up",
            "ec_high": "ec_down",
        }.get(prior_decision, "none")

    has_pump = pump.strip().lower() not in {"", "none"}
    if dose_ml is not None and has_pump:
        return f" after {dose_ml:.2f} mL {_pump_label(pump)}"
    if has_pump:
        return f" on the {_pump_label(pump)} line"
    if dose_ml is not None:
        return f" after a {dose_ml:.2f} mL dose"
    return ""


def _companion_reading_text(
    payload: SensorPayload,
    decision: DosingDecision,
    *,
    include_reservoir: bool = True,
) -> str:
    """Include the other controlled measurement without crowding the SMS."""
    volume = f"{payload.reservoir_volume_liters:.1f}".rstrip("0").rstrip(".")
    environment_parts = [f"water temperature {payload.temperature:.1f} C"]
    if include_reservoir:
        environment_parts.append(f"reservoir {volume} L")
    environment = f"{', '.join(environment_parts)}."
    if decision.decision.startswith("ph_"):
        return f"EC {payload.ec:.2f} mS/cm, {environment}"
    if decision.decision.startswith("ec_"):
        return f"pH {payload.ph:.2f}, {environment}"
    return environment


def _sms_severity(decision: DosingDecision) -> str:
    """Classify SMS urgency from risk and agentic context."""
    risk_flags = set(_string_list(decision.metadata.get("risk_flags")))
    if risk_flags.intersection({"sensor_anomaly", "possible_delivery_issue", "human_review_required"}):
        return "check_system"
    if decision.metadata.get("dose_cap_hit") is True:
        return "check_system"
    if not decision.ph_within_range and not decision.ec_within_range:
        return "check_system"

    same_pump = _metadata_dict(decision.metadata.get("same_pump_response"))
    interpretation = str(same_pump.get("interpretation") or "")
    if interpretation in {
        "previous_same_pump_still_unresolved",
        "previous_same_pump_overshot_opposite_direction",
    }:
        return "check_system"

    return "action_taken"


def _operator_check_action(decision: DosingDecision) -> str:
    """Return the operator-facing check instruction for higher-severity SMS."""
    risk_flags = set(_string_list(decision.metadata.get("risk_flags")))
    if "sensor_anomaly" in risk_flags:
        return "Check probes before next correction."
    if "possible_delivery_issue" in risk_flags or _delivery_issue_detected(decision):
        return "Inspect solution line and pump."
    if decision.metadata.get("dose_cap_hit") is True:
        return "Dose capped; monitor next reading."

    same_pump = _metadata_dict(decision.metadata.get("same_pump_response"))
    interpretation = str(same_pump.get("interpretation") or "")
    if interpretation == "previous_same_pump_still_unresolved":
        return "Wait for response before redosing."
    if interpretation == "previous_same_pump_overshot_opposite_direction":
        return "Check probe before redosing."
    if not decision.ph_within_range and not decision.ec_within_range:
        return "Watch next reading."
    return "Review dashboard when available."


def _metadata_dict(value: object) -> dict[str, object]:
    """Return metadata object when it is a dict."""
    return value if isinstance(value, dict) else {}


def _pump_label(pump: str) -> str:
    """Return a phone-friendly pump label."""
    labels = {
        "ph_up": "pH Up",
        "ph_down": "pH Down",
        "ec_up": "EC Up",
        "ec_down": "EC Down",
        "none": "None",
        "": "None",
    }
    return labels.get(pump, pump.replace("_", " ").title())


def _alert_title(alert_type: str) -> str:
    """Return an operator-facing alert title."""
    titles = {
        "sensor_anomaly": "Sensor anomaly",
        "possible_delivery_issue": "Possible pump delivery issue",
        "human_review_required": "Operator review needed",
        "wait_safety_gate": "Safety gate hold",
        "error": "Controller error",
    }
    return titles.get(alert_type, alert_type.replace("_", " ").title())


def _string_list(value: object) -> list[str]:
    """Return a list of strings from a metadata field."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _delivery_issue_detected(decision: DosingDecision) -> bool:
    """Return whether decision metadata reports a delivery issue."""
    delivery_issue = decision.metadata.get("delivery_issue")
    return isinstance(delivery_issue, dict) and delivery_issue.get("issue_detected") is True
