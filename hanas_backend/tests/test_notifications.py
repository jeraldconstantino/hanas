"""Tests for optional SMS notification behavior."""

from app.core.config import settings
from app.schemas.sensor import DosingDecision, SensorPayload
from app.services import notifications
from app.services.notifications import maybe_send_decision_alert


class FakeNotificationLogWriter:
    """Collect notification log writes for assertions."""

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.system_settings: dict[str, object] = {}

    def create_notification_log(self, **kwargs: object) -> None:
        """Store one notification log row."""
        self.rows.append(kwargs)

    def get_system_setting(self, key: str) -> object:
        return self.system_settings.get(key)


def _payload() -> SensorPayload:
    return SensorPayload(
        temperature=25,
        ph=5.0,
        ec=1.8,
        reservoir_volume_liters=20,
        control_strategy="agentic_ai",
    )


def _decision(**metadata: object) -> DosingDecision:
    return DosingDecision(
        decision="ph_low",
        pump_activated="ph_up",
        dose_ml=10,
        duration_ms=4959,
        ph_within_range=False,
        ec_within_range=True,
        ph_deviation=0.5,
        ec_deviation=0,
        reason="pH is below the configured target range.",
        metadata=dict(metadata),
    )


def _enable_sms(monkeypatch) -> None:
    monkeypatch.setattr(settings, "sms_notifications_enabled", True)
    monkeypatch.setattr(settings, "sms_receiver_phone_number", "+639000000000")
    monkeypatch.setattr(settings, "sms_sender_id", "HANAS")
    monkeypatch.setattr(settings, "semaphore_api_key", "secret")
    monkeypatch.setattr(settings, "sms_alert_cooldown_seconds", 900)
    notifications._last_sent_at_by_key.clear()


def test_normal_decision_does_not_send_sms(monkeypatch) -> None:
    """Normal monitoring decisions should not notify the operator."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = DosingDecision(
        decision="within_range",
        pump_activated="none",
        dose_ml=0,
        duration_ms=0,
        ph_within_range=True,
        ec_within_range=True,
        ph_deviation=0,
        ec_deviation=0,
        reason="pH and EC are within target range.",
        metadata={},
    )

    sent = maybe_send_decision_alert(_payload(), decision, log_id=1, control_cycle_id=2)

    assert sent is False
    assert sent_messages == []


def test_monitoring_mode_suppresses_sms_after_a_decision_was_built(monkeypatch) -> None:
    """A batch finishing during a mode transition must not notify or act."""
    _enable_sms(monkeypatch)
    writer = FakeNotificationLogWriter()
    writer.system_settings["monitoring_mode_enabled"] = True
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)

    sent = maybe_send_decision_alert(
        _payload(),
        _decision(risk_flags=["possible_delivery_issue"]),
        log_id=1,
        control_cycle_id=2,
        notification_log_writer=writer,
    )

    assert sent is False
    assert sent_messages == []
    assert writer.rows == []


def test_dosing_decision_sends_sms_when_configured(monkeypatch) -> None:
    """Dosing commands should produce an SMS confirmation."""
    _enable_sms(monkeypatch)
    writer = FakeNotificationLogWriter()
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = _decision(
        mixing_window={
            "effective_seconds": 240,
        }
    )

    sent = maybe_send_decision_alert(
        _payload(),
        decision,
        log_id=1,
        control_cycle_id=2,
        notification_log_writer=writer,
    )

    assert sent is True
    assert len(sent_messages) == 1
    assert sent_messages[0] == (
        "HANAS: I scheduled 10.00 mL pH Up for low pH at 5.00. "
        "EC 1.80 mS/cm, water temperature 25.0 C, reservoir 20 L. "
        "Mix 240 s. Then I will recheck."
    )
    assert len(sent_messages[0]) <= notifications.SMS_SINGLE_SEGMENT_CHARS
    assert ";" not in sent_messages[0]
    assert "—" not in sent_messages[0]
    assert writer.rows[0]["status"] == "submitted"
    assert writer.rows[0]["alert_type"] == "dosing_started"
    assert writer.rows[0]["system_log_id"] == 1
    assert writer.rows[0]["control_cycle_id"] == 2


def test_dosing_sms_uses_agentic_context_when_available(monkeypatch) -> None:
    """Agentic metadata should make SMS alerts explain the decision context."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = DosingDecision(
        decision="ph_high",
        pump_activated="ph_down",
        dose_ml=10,
        duration_ms=4959,
        ph_within_range=False,
        ec_within_range=False,
        ph_deviation=0.72,
        ec_deviation=0.05,
        reason="pH is above the configured target range.",
        metadata={
            "mixing_window": {"effective_seconds": 300},
            "monitoring_agent": {
                "summary": "pH is high and EC is low; readings are stable.",
            },
            "diagnostic_reasoning_agent": {
                "classification": "combined_disturbance",
                "primary_metric": "ph",
            },
            "same_pump_response": {
                "interpretation": "previous_same_pump_overshot_opposite_direction",
            },
            "consistency_review": {
                "review_status": "pass",
            },
        },
    )

    sent = maybe_send_decision_alert(
        SensorPayload(
            temperature=30.4,
            ph=7.22,
            ec=1.15,
            reservoir_volume_liters=70,
            control_strategy="agentic_ai",
        ),
        decision,
        log_id=10,
        control_cycle_id=20,
    )

    assert sent is True
    assert sent_messages[0] == (
        "HANAS: Both outside target: pH 7.22, EC 1.15 mS/cm. "
        "I scheduled 10.00 mL pH Down. Water temperature 30.4 C. "
        "Mix 300 s. Check probe before redosing."
    )
    assert len(sent_messages[0]) <= notifications.SMS_SINGLE_SEGMENT_CHARS
    assert not sent_messages[0].endswith("...")


def test_all_single_metric_dosing_states_use_the_correct_message(monkeypatch) -> None:
    """Every high/low control direction should name the condition and commanded pump."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    cases = [
        ("ph_low", "ph_up", 5.0, 1.5, "pH Up", "low pH at 5.00"),
        ("ph_high", "ph_down", 7.0, 1.5, "pH Down", "high pH at 7.00"),
        ("ec_low", "ec_up", 6.0, 0.8, "each of EC Up A/B", "low EC at 0.80 mS/cm"),
        ("ec_high", "ec_down", 6.0, 2.5, "EC Down", "high EC at 2.50 mS/cm"),
    ]

    for index, (state, pump, ph, ec, command, condition) in enumerate(cases):
        decision = DosingDecision(
            decision=state,
            pump_activated=pump,
            dose_ml=2,
            duration_ms=1200,
            ph_within_range=not state.startswith("ph_"),
            ec_within_range=not state.startswith("ec_"),
            ph_deviation=0.5 if state.startswith("ph_") else 0,
            ec_deviation=0.5 if state.startswith("ec_") else 0,
            reason="Outside target.",
            metadata={"mixing_window": {"effective_seconds": 225}},
        )
        payload = SensorPayload(
            temperature=26.1,
            ph=ph,
            ec=ec,
            reservoir_volume_liters=32.8,
            control_strategy="agentic_ai",
        )

        assert maybe_send_decision_alert(
            payload,
            decision,
            log_id=100 + index,
            control_cycle_id=200 + index,
        ) is True
        message = sent_messages[-1]
        assert command in message
        assert condition in message
        assert "water temperature 26.1 C" in message
        assert len(message) <= notifications.SMS_SINGLE_SEGMENT_CHARS
        assert not message.endswith("...")


def test_combined_disturbance_reports_both_readings_and_one_correction(monkeypatch) -> None:
    """Mixed pH/EC conditions should be explicit without implying two simultaneous doses."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = DosingDecision(
        decision="ec_high",
        pump_activated="ec_down",
        dose_ml=3,
        duration_ms=1500,
        ph_within_range=False,
        ec_within_range=False,
        ph_deviation=0.2,
        ec_deviation=0.4,
        reason="Combined disturbance; correct EC first.",
        metadata={"mixing_window": {"effective_seconds": 225}},
    )
    payload = SensorPayload(
        temperature=26.3,
        ph=5.3,
        ec=2.6,
        reservoir_volume_liters=35.6,
        control_strategy="agentic_ai",
    )

    assert maybe_send_decision_alert(payload, decision, log_id=110, control_cycle_id=210) is True
    message = sent_messages[0]
    assert "3.00 mL EC Down" in message
    assert "Both outside target: pH 5.30, EC 2.60 mS/cm" in message
    assert "Watch next reading." in message
    assert len(message) <= notifications.SMS_SINGLE_SEGMENT_CHARS
    assert not message.endswith("...")


def test_routine_agentic_dosing_sms_stays_action_taken(monkeypatch) -> None:
    """Single-metric agentic dosing should stay concise when no risk is present."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = _decision(
        mixing_window={"effective_seconds": 300},
        monitoring_agent={"summary": "pH is below target while EC remains acceptable."},
        diagnostic_reasoning_agent={"classification": "ph_low", "primary_metric": "ph"},
        consistency_review={"review_status": "pass"},
    )

    sent = maybe_send_decision_alert(_payload(), decision, log_id=11, control_cycle_id=22)

    assert sent is True
    assert sent_messages[0] == (
        "HANAS: I scheduled 10.00 mL pH Up for low pH at 5.00. "
        "EC 1.80 mS/cm, water temperature 25.0 C, reservoir 20 L. "
        "Mix 300 s. Then I will recheck."
    )


def test_dosing_sms_keeps_full_assistant_message_for_live_sized_values(monkeypatch) -> None:
    """Typical decimal readings must not truncate the assistant's follow-up."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = DosingDecision(
        decision="ph_high",
        pump_activated="ph_down",
        dose_ml=1.97,
        duration_ms=1200,
        ph_within_range=False,
        ec_within_range=True,
        ph_deviation=0.04,
        ec_deviation=0,
        reason="pH is above the configured target range.",
        metadata={"mixing_window": {"effective_seconds": 225}},
    )
    payload = SensorPayload(
        temperature=27.1,
        ph=6.54,
        ec=1.43,
        reservoir_volume_liters=34.3,
        control_strategy="agentic_ai",
    )

    assert maybe_send_decision_alert(payload, decision, log_id=14, control_cycle_id=25) is True
    assert sent_messages[0] == (
        "HANAS: I scheduled 1.97 mL pH Down for high pH at 6.54. "
        "EC 1.43 mS/cm, water temperature 27.1 C, reservoir 34.3 L. "
        "Mix 225 s. Then I will recheck."
    )
    assert len(sent_messages[0]) <= notifications.SMS_SINGLE_SEGMENT_CHARS
    assert not sent_messages[0].endswith("...")


def test_ec_up_sms_reports_each_component_and_stays_one_segment(monkeypatch) -> None:
    """EC-up messages must not imply the recorded dose is the combined A/B volume."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = DosingDecision(
        decision="ec_low",
        pump_activated="ec_up",
        dose_ml=10,
        duration_ms=4800,
        ph_within_range=True,
        ec_within_range=False,
        ph_deviation=0,
        ec_deviation=0.2,
        reason="EC is below the configured target range.",
        metadata={"mixing_window": {"effective_seconds": 240}},
    )
    payload = SensorPayload(
        temperature=25,
        ph=6,
        ec=1,
        reservoir_volume_liters=20,
        control_strategy="agentic_ai",
    )

    assert maybe_send_decision_alert(payload, decision, log_id=13, control_cycle_id=24) is True
    assert "10.00 mL each of EC Up A/B" in sent_messages[0]
    assert len(sent_messages[0]) <= notifications.SMS_SINGLE_SEGMENT_CHARS
    assert sent_messages[0].isascii()
    assert ";" not in sent_messages[0]
    assert "—" not in sent_messages[0]


def test_dosing_sms_stays_conversational_with_llm_trace(monkeypatch) -> None:
    """LLM-backed dosing should still produce one concise operator notification."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = _decision(
        llm_trace=[
            {
                "agent": "decision_agent",
                "source": "llm",
                "model": "test-model",
                "output": {
                    "action": "dose",
                    "reason": "The stable pH-low trend requires a bounded correction.",
                },
            }
        ],
    )

    sent = maybe_send_decision_alert(_payload(), decision, log_id=12, control_cycle_id=23)

    assert sent is True
    assert sent_messages[0] == (
        "HANAS: I scheduled 10.00 mL pH Up for low pH at 5.00. "
        "EC 1.80 mS/cm, water temperature 25.0 C, reservoir 20 L. "
        "I'll check the next reading."
    )
    assert "AI note:" not in sent_messages[0]
    assert "make mistakes" not in sent_messages[0]
    assert len(sent_messages[0]) <= notifications.SMS_SINGLE_SEGMENT_CHARS
    assert sent_messages[0].isascii()


def test_delivery_issue_sends_sms_when_configured(monkeypatch) -> None:
    """Possible delivery issues should produce an SMS alert."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)

    sent = maybe_send_decision_alert(
        _payload(),
        _decision(risk_flags=["possible_delivery_issue"]),
        log_id=123,
        control_cycle_id=456,
    )

    assert sent is True
    assert len(sent_messages) == 1
    assert "Possible pump delivery issue detected" in sent_messages[0]
    assert "10.00 mL pH Up" in sent_messages[0]
    assert "water temperature 25.0 C" in sent_messages[0]
    assert sent_messages[0].endswith("Please inspect the solution line and pump.")
    assert len(sent_messages[0]) <= notifications.SMS_SINGLE_SEGMENT_CHARS


def test_delivery_issue_uses_previous_dose_when_current_command_is_empty(monkeypatch) -> None:
    """Delivery alerts should describe the prior dose instead of saying 0.00 mL None."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = _decision(
        risk_flags=["possible_delivery_issue"],
        delivery_issue={
            "issue_detected": True,
            "latest_same_pump_dose_ml": 14.96,
            "latest_same_pump_decision": "ph_high",
            "current_pump": "ph_down",
        },
    ).model_copy(
        update={
            "decision": "within_range",
            "pump_activated": "none",
            "dose_ml": 0,
            "duration_ms": 0,
        }
    )

    sent = maybe_send_decision_alert(_payload(), decision, log_id=7, control_cycle_id=8)

    assert sent is True
    assert "after 14.96 mL pH Down" in sent_messages[0]
    assert "0.00 mL" not in sent_messages[0]
    assert "None" not in sent_messages[0]


def test_delivery_issue_omits_command_when_no_valid_dose_details_exist(monkeypatch) -> None:
    """A metadata-poor delivery alert should remain grammatical and actionable."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = _decision(risk_flags=["possible_delivery_issue"]).model_copy(
        update={
            "decision": "within_range",
            "pump_activated": "none",
            "dose_ml": 0,
            "duration_ms": 0,
        }
    )

    sent = maybe_send_decision_alert(_payload(), decision, log_id=9, control_cycle_id=10)

    assert sent is True
    assert sent_messages[0].startswith("HANAS: Possible pump delivery issue detected. pH")
    assert "0.00 mL" not in sent_messages[0]
    assert "None" not in sent_messages[0]


def test_sensor_anomaly_sends_sms_when_configured(monkeypatch) -> None:
    """Sensor anomaly decisions should notify the operator."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = _decision(risk_flags=["sensor_anomaly"])
    decision.decision = "sensor_anomaly"
    decision.pump_activated = "none"
    decision.dose_ml = 0
    decision.duration_ms = 0

    sent = maybe_send_decision_alert(_payload(), decision, log_id=3, control_cycle_id=4)

    assert sent is True
    assert "Unusual sensor readings detected" in sent_messages[0]
    assert "water temperature 25.0 C" in sent_messages[0]
    assert "verify the probes and reservoir" in sent_messages[0]


def test_human_review_required_sends_sms_when_configured(monkeypatch) -> None:
    """HITL waits should notify the researcher/operator."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = _decision(risk_flags=["human_review_required"])
    decision.decision = "wait_human_review"
    decision.pump_activated = "none"
    decision.dose_ml = 0
    decision.duration_ms = 0

    sent = maybe_send_decision_alert(_payload(), decision, log_id=5, control_cycle_id=6)

    assert sent is True
    assert "I need your review before I activate a pump" in sent_messages[0]
    assert "cycle #6" in sent_messages[0]


def test_sms_cooldown_suppresses_repeat_alert(monkeypatch) -> None:
    """Repeated matching alerts are rate-limited to avoid SMS spam."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = _decision(risk_flags=["possible_delivery_issue"])

    assert maybe_send_decision_alert(_payload(), decision, log_id=1, control_cycle_id=2) is True
    assert maybe_send_decision_alert(_payload(), decision, log_id=3, control_cycle_id=4) is False
    assert len(sent_messages) == 1


def test_sms_cooldown_suppresses_repeat_dosing_alert(monkeypatch) -> None:
    """Repeated same-pump dosing alerts are rate-limited across control cycles."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)
    decision = _decision()

    assert maybe_send_decision_alert(_payload(), decision, log_id=1, control_cycle_id=2) is True
    assert maybe_send_decision_alert(_payload(), decision, log_id=3, control_cycle_id=4) is False
    assert len(sent_messages) == 1


def test_sms_disabled_suppresses_alert(monkeypatch) -> None:
    """Alerts are not sent unless explicitly enabled and configured."""
    _enable_sms(monkeypatch)
    monkeypatch.setattr(settings, "sms_notifications_enabled", False)
    writer = FakeNotificationLogWriter()
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)

    sent = maybe_send_decision_alert(
        _payload(),
        _decision(risk_flags=["possible_delivery_issue"]),
        log_id=1,
        control_cycle_id=2,
        notification_log_writer=writer,
    )

    assert sent is False
    assert sent_messages == []
    assert writer.rows[0]["status"] == "disabled"


def test_sms_failure_is_logged(monkeypatch) -> None:
    """Failed provider calls should be persisted for audit/debugging."""
    _enable_sms(monkeypatch)
    writer = FakeNotificationLogWriter()

    def fail_send(_: str) -> str:
        raise OSError("provider unavailable")

    monkeypatch.setattr(notifications, "_send_sms", fail_send)

    sent = maybe_send_decision_alert(
        _payload(),
        _decision(risk_flags=["possible_delivery_issue"]),
        log_id=1,
        control_cycle_id=2,
        notification_log_writer=writer,
    )

    assert sent is False
    assert writer.rows[0]["status"] == "failed"
    assert writer.rows[0]["error_message"] == "provider unavailable"


def test_semaphore_configuration_sends_sms_when_configured(monkeypatch) -> None:
    """Semaphore can be selected as the alert provider for Philippine numbers."""
    _enable_sms(monkeypatch)
    sent_messages: list[str] = []
    monkeypatch.setattr(notifications, "_send_sms", sent_messages.append)

    sent = maybe_send_decision_alert(
        _payload(),
        _decision(risk_flags=["possible_delivery_issue"]),
        log_id=1,
        control_cycle_id=2,
    )

    assert sent is True
    assert "Possible pump delivery issue detected" in sent_messages[0]


def test_semaphore_recipient_normalization() -> None:
    """Semaphore examples use Philippine recipients in local mobile format."""
    assert notifications._semaphore_recipient("+639171234567") == "09171234567"
    assert notifications._semaphore_recipient("639171234567") == "09171234567"
    assert notifications._semaphore_recipient("09171234567") == "09171234567"


def test_semaphore_pending_response_is_persisted_with_provider_id(monkeypatch) -> None:
    """A successful POST records Semaphore's state instead of assuming delivery."""
    _enable_sms(monkeypatch)
    writer = FakeNotificationLogWriter()
    response = '[{"message_id": 278522315, "status": "Pending"}]'
    monkeypatch.setattr(notifications, "_send_sms", lambda _body: response)

    sent = maybe_send_decision_alert(
        _payload(),
        _decision(risk_flags=["possible_delivery_issue"]),
        log_id=1,
        control_cycle_id=2,
        notification_log_writer=writer,
    )

    assert sent is True
    assert writer.rows[0]["status"] == "pending"
    assert writer.rows[0]["provider_message_id"] == 278522315
    assert writer.rows[0]["provider_response"] == response
