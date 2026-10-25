from astinuslab.ui import volatility_routes


def test_isf_scenarios_offer_only_enabled_immediate_admission(monkeypatch):
    scenarios = [
        {"id": "now", "mode": "immediate", "enabled": True},
        {"id": "later", "mode": "post_script", "enabled": True},
        {"id": "manual", "mode": "manual", "enabled": True},
        {"id": "disabled", "mode": "immediate", "enabled": False},
    ]
    monkeypatch.setattr(volatility_routes, "list_scenarios_service", lambda: scenarios)
    assert volatility_routes._isf_scenarios() == [scenarios[0]]
