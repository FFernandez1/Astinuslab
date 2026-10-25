from __future__ import annotations


def test_settings_has_celery_broker_url():
    from astinuslab.core.config import settings

    assert hasattr(settings, "celery_broker_url")
    assert settings.celery_broker_url.startswith("sqla+sqlite://")


def test_settings_has_celery_result_backend():
    from astinuslab.core.config import settings

    assert hasattr(settings, "celery_result_backend")
    assert settings.celery_result_backend.startswith("db+sqlite://")


def test_settings_has_reconciliation_defaults():
    from astinuslab.core.config import settings

    assert settings.celery_reconciliation_enabled is True
    assert settings.celery_reconciliation_interval_seconds == 60
    assert settings.celery_reconciliation_grace_seconds == 120
