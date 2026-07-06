import json

import pytest

from app.config import Config, ConfigurationError

from .helpers import make_config


def test_polling_interval_must_be_positive(tmp_path):
    with pytest.raises(ConfigurationError):
        make_config(tmp_path, polling_interval=0)


def test_email_export_requires_email_settings(tmp_path):
    with pytest.raises(ConfigurationError):
        make_config(tmp_path, enable_email_export=True)


def test_email_export_with_settings_ok(tmp_path):
    config = make_config(
        tmp_path,
        enable_email_export=True,
        email_from="a@example.com",
        email_to="b@example.com",
        email_server="mail.example.com",
    )
    assert config.enable_email_export


def test_from_ha_invalid_json_raises(tmp_path):
    options = tmp_path / "options.json"
    options.write_text("{not json")
    with pytest.raises(ConfigurationError):
        Config.from_ha(options)


def test_from_env_bad_number_raises_configuration_error(monkeypatch):
    monkeypatch.setenv("POLLING_INTERVAL", "abc")
    with pytest.raises(ConfigurationError):
        Config.from_env()


def test_from_ha_bad_number_raises_configuration_error(tmp_path):
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"polling_interval": "abc"}))
    with pytest.raises(ConfigurationError):
        Config.from_ha(options)


def test_from_ha_parses_options(tmp_path):
    options = tmp_path / "options.json"
    options.write_text(
        json.dumps(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "polling_interval": 30,
                "email": {"port": 25},
            }
        )
    )
    config = Config.from_ha(options)
    assert config.homeassistant is True
    assert config.polling_interval == 30
    assert config.email_server_port == 25
    assert config.env_access_token == "at"
