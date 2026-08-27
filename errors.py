"""Public error contracts shared by Haven HTTP and MCP surfaces.

This module is intentionally small.  It ports only the P0luz behavior that
prevents provider details and credentials from escaping through public error
messages; Haven keeps its existing response envelopes and logging topology.
"""
from __future__ import annotations

import re

PUBLIC_MESSAGE_MAX = 500
SAFE_DETAIL_MAX = 200
_REDACTED = "[REDACTED]"
_REDACTED_URL = "[REDACTED_URL]"


class PublicToolError(RuntimeError):
    """An error whose fixed ``public_message`` is safe to return to clients.

    The base exception message remains generic so an accidental ``str(exc)``
    does not expose the approved client-facing text or a chained provider
    exception.
    """

    def __init__(self, public_message: str):
        message = str(public_message).strip()
        if (
            not message
            or len(message) > PUBLIC_MESSAGE_MAX
            or any(ord(char) < 32 for char in message)
        ):
            raise ValueError("公开工具错误文案必须是单行安全文本")
        self.public_message = message
        super().__init__("public tool error")


def safe_error_detail(exc: BaseException) -> str:
    """Return a bounded, single-line and credential-free exception detail."""

    try:
        detail = str(exc).strip()
    except Exception:
        detail = ""
    detail = detail or type(exc).__name__
    detail = re.sub(r"[\x00-\x1f\x7f]+", " ", detail)
    detail = re.sub(r"\s+", " ", detail).strip()

    # Authorization values and common provider key forms.
    detail = re.sub(
        r"(?i)(\bbearer\s+)[^\s,;]+",
        rf"\1{_REDACTED}",
        detail,
    )
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", _REDACTED, detail)
    detail = re.sub(
        r"(?i)((?:authorization|x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)\s*[=:]\s*)[^\s,;]+",
        rf"\1{_REDACTED}",
        detail,
    )

    # Provider/private URLs are not useful in a public failure and can carry
    # query credentials, internal hostnames or signed paths.
    detail = re.sub(r"(?i)https?://[^\s,;]+", _REDACTED_URL, detail)
    return detail[:SAFE_DETAIL_MAX]


def llm_step_failed_error(step_zh: str, *, api_available: bool) -> PublicToolError:
    """Build a fixed public failure for an LLM-backed write step.

    ``api_available`` only means that configuration is present.  It does not
    prove that a key is valid, funded, or accepted by the provider.
    """

    if not api_available:
        return PublicToolError(
            f"脱水 API 不可用（未配置或配置有误），{step_zh}无法完成，桶未创建。"
            "请检查 OMBRE_COMPRESS_API_KEY 与 config.yaml 的 dehydration 配置。"
        )
    return PublicToolError(
        f"脱水 API 调用失败或返回无法解析的内容，{step_zh}无法完成，桶未创建。"
        "可稍后重试；持续失败请看 server.log 里的 err_type，"
        "再逐一排除供应商故障、模型返回为空、key 失效或余额不足。"
    )
