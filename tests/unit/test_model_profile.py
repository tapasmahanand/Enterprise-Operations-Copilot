import pytest

from kestrel.llm import model_for


def test_production_is_the_default(monkeypatch):
    for k in ("MODEL_PROFILE", "SUPERVISOR_MODEL", "COMPOSER_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert model_for("supervisor") == model_for("composer") == "gpt-5.4-mini"


def test_demo_profile_uses_gpt_5_4_and_role_override_still_wins(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_MODEL", raising=False)
    monkeypatch.setenv("MODEL_PROFILE", "demo")
    monkeypatch.setenv("COMPOSER_MODEL", "gpt-5.4-mini")
    assert model_for("supervisor") == "gpt-5.4" and model_for("composer") == "gpt-5.4-mini"
    assert model_for("rewrite") == "gpt-5.4-nano"


def test_unknown_profile_fails_loudly(monkeypatch):
    monkeypatch.setenv("MODEL_PROFILE", "demoo")
    with pytest.raises(ValueError):
        model_for("supervisor")
