"""Phase 1.5: secret redaction on model-bound tool output and logs (ADR-024).

DoD: a known secret must not appear in model-bound messages or in logs.
"""
import logging

from src import redaction
from src.redaction import RedactingLogFilter, redact


def setup_function(_):
    # Tests mutate os.environ; always rebuild the env-secret cache fresh.
    redaction.refresh_secret_values()


def teardown_function(_):
    redaction.refresh_secret_values()


def test_redacts_structural_patterns():
    cases = [
        "here is my key sk-ABCDEFGHIJKLMNOPQRSTUVWX1234567890",
        "anthropic sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA",
        "app token ody_AbCdEf012345_67890ghijkl",
        "github ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "aws AKIAIOSFODNN7EXAMPLE here",
        "Authorization: Bearer eyJhbGciOi.AAAAAAAAAAAAAAAAAAAA.bbbb",
    ]
    for c in cases:
        out = redact(c)
        assert "[REDACTED]" in out, c
    # The secret material itself is gone.
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX1234567890" not in redact(cases[0])
    assert "AKIAIOSFODNN7EXAMPLE" not in redact(cases[4])


def test_redacts_pem_private_key_block():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890abcdefSECRETKEYMATERIAL\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact(f"deploy key:\n{pem}\nend")
    assert "SECRETKEYMATERIAL" not in out
    assert "[REDACTED]" in out


def test_redacts_known_env_secret_value(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "supersecretvalue-7f3a9c2b1e")
    redaction.refresh_secret_values()
    out = redact("the configured key is supersecretvalue-7f3a9c2b1e in use")
    assert "supersecretvalue-7f3a9c2b1e" not in out
    assert "[REDACTED]" in out


def test_does_not_over_redact_plain_text():
    plain = "The build finished in 12 seconds with exit_code 0 and 3 warnings."
    assert redact(plain) == plain


def test_short_env_values_and_config_flags_not_treated_as_secret(monkeypatch):
    # A flag-ish value (short / allowlisted name) must not be masked - else
    # redaction would scrub harmless output everywhere that value appears.
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("SOME_API_KEY", "x")  # too short to be a credential
    redaction.refresh_secret_values()
    assert redact("auth is true and x marks the spot") == "auth is true and x marks the spot"


def test_redact_is_exception_safe():
    # Non-str input must not raise.
    assert redact(None) is None
    assert redact(12345) == "12345" or redact(12345) == 12345


def test_format_tool_result_redacts_secret_in_output(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tok-DEADBEEFcafef00d12345")
    redaction.refresh_secret_values()
    from src.tool_execution import format_tool_result

    out = format_tool_result(
        "bash",
        {"output": "echoing token tok-DEADBEEFcafef00d12345 and key sk-ABCDEFGHIJKLMNOPQRSTUVWX12", "exit_code": 0},
    )
    assert "tok-DEADBEEFcafef00d12345" not in out
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX12" not in out
    assert "[REDACTED]" in out


def test_log_filter_redacts_record():
    filt = RedactingLogFilter()
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="leaked token=%s", args=("ody_AbCdEf012345_67890ghijkl",), exc_info=None,
    )
    assert filt.filter(rec) is True
    assert "ody_AbCdEf012345_67890ghijkl" not in rec.getMessage()
    assert "[REDACTED]" in rec.getMessage()
