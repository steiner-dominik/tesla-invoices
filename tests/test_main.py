import os
import time

import app.main as main


def _restore_tz(original):
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def test_timezone_fetched_from_supervisor(monkeypatch):
    original = os.environ.get("TZ")
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "token")

    requested = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"timezone": "Europe/Vienna"}}

    def fake_get(url, headers=None, timeout=None):
        requested["url"] = url
        requested["auth"] = headers["Authorization"]
        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)
    try:
        main._apply_home_assistant_timezone()
        assert os.environ["TZ"] == "Europe/Vienna"
        assert requested["url"] == main.SUPERVISOR_INFO_URL
        assert requested["auth"] == "Bearer token"
    finally:
        _restore_tz(original)


def test_explicit_tz_wins_over_supervisor(monkeypatch):
    # A user-provided TZ (standalone deployments) must never be overridden
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "token")

    def fail(*_args, **_kwargs):
        raise AssertionError("the Supervisor API must not be queried when TZ is set")

    monkeypatch.setattr("requests.get", fail)
    main._apply_home_assistant_timezone()
    assert os.environ["TZ"] == "UTC"


def test_standalone_without_supervisor_is_noop(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    main._apply_home_assistant_timezone()
    assert "TZ" not in os.environ


def test_language_fetched_from_core_api(monkeypatch):
    monkeypatch.delenv("LANGUAGE", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "token")

    requested = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"language": "de"}

    def fake_get(url, headers=None, timeout=None):
        requested["url"] = url
        requested["auth"] = headers["Authorization"]
        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)
    main._apply_home_assistant_language()
    assert os.environ["LANGUAGE"] == "de"
    assert requested["url"] == main.CORE_CONFIG_URL
    assert requested["auth"] == "Bearer token"
    monkeypatch.delenv("LANGUAGE")


def test_explicit_language_wins_over_core_api(monkeypatch):
    # A user-provided LANGUAGE (standalone deployments) must never be overridden
    monkeypatch.setenv("LANGUAGE", "en")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "token")

    def fail(*_args, **_kwargs):
        raise AssertionError("the Core API must not be queried when LANGUAGE is set")

    monkeypatch.setattr("requests.get", fail)
    main._apply_home_assistant_language()
    assert os.environ["LANGUAGE"] == "en"


def test_language_without_supervisor_is_noop(monkeypatch):
    monkeypatch.delenv("LANGUAGE", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    main._apply_home_assistant_language()
    assert "LANGUAGE" not in os.environ
