import json
import logging

import pytest

from errors import PublicToolError, llm_step_failed_error, safe_error_detail
from gateway import GatewayService


@pytest.mark.parametrize(
    "message, forbidden",
    [
        (
            "call failed: Authorization: Bearer abc123secret, api_key=sk-live_secret_123456 https://provider.invalid/private",
            ("abc123secret", "sk-live_secret_123456", "provider.invalid"),
        ),
        (
            "token: top-secret-value; X-API-Key=another-secret-value; password=hunter2",
            ("top-secret-value", "another-secret-value", "hunter2"),
        ),
    ],
)
def test_safe_error_detail_redacts_credentials_and_urls(message, forbidden):
    detail = safe_error_detail(RuntimeError(message))

    for secret in forbidden:
        assert secret not in detail
    assert "[REDACTED]" in detail
    assert "call failed" in detail or "token" in detail


def test_safe_error_detail_is_single_line_bounded_and_has_fallback():
    detail = safe_error_detail(RuntimeError("line one\nline two\t" + "x" * 5000))

    assert "\n" not in detail
    assert "\t" not in detail
    assert len(detail) <= 200
    assert safe_error_detail(RuntimeError("")) == "RuntimeError"


@pytest.mark.parametrize("message", ("", "line one\nline two", "control\x1btext", "x" * 501))
def test_public_tool_error_rejects_unsafe_public_messages(message):
    with pytest.raises(ValueError):
        PublicToolError(message)


def test_public_tool_error_keeps_runtime_message_generic():
    error = PublicToolError("可以安全公开的固定文案")

    assert error.public_message == "可以安全公开的固定文案"
    assert str(error) == "public tool error"


def test_llm_step_failure_message_only_blames_configuration_when_unavailable():
    unavailable = llm_step_failed_error("日记拆分", api_available=False)
    provider_failure = llm_step_failed_error("日记拆分", api_available=True)

    assert "OMBRE_COMPRESS_API_KEY" in unavailable.public_message
    assert "桶未创建" in unavailable.public_message
    assert "OMBRE_COMPRESS_API_KEY" not in provider_failure.public_message
    assert "key 配置正常" not in provider_failure.public_message
    assert "桶未创建" in provider_failure.public_message


@pytest.mark.asyncio
async def test_gateway_health_error_preserves_envelope_without_leaking_secret(caplog):
    service = object.__new__(GatewayService)
    secret = "sk-provider-secret-123456"
    private_url = "https://provider.invalid/private"

    async def fail_health():
        raise RuntimeError(
            f"upstream failed Authorization: Bearer bearer-secret api_key={secret} {private_url}"
        )

    service.health_payload = fail_health
    with caplog.at_level(logging.ERROR, logger="ombre_brain.gateway"):
        response = await service.handle_health(None)

    payload = json.loads(response.body)
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)

    assert response.status_code == 500
    assert payload["status"] == "error"
    assert payload["detail"]
    for leaked in (secret, "bearer-secret", "provider.invalid"):
        assert leaked not in payload["detail"]
        assert leaked not in rendered_logs
