from app.privacy import redact_vin


def test_redacts_vin_in_charging_filename():
    name = "tesla_charging_invoice_5YJ3E7EB9KF000316_2026-07-01_AT_INV123.pdf"
    assert redact_vin(name) == "tesla_charging_invoice_…0316_2026-07-01_AT_INV123.pdf"


def test_redacts_vin_in_subscription_filename():
    name = "tesla_subscription_invoice_XP7YGCEL9RB123456_2026-07-01.pdf"
    assert redact_vin(name) == "tesla_subscription_invoice_…3456_2026-07-01.pdf"


def test_redacts_vin_in_full_path():
    path = "/opt/tesla-invoices/invoices/tesla_charging_invoice_5YJ3E7EB9KF000316_2026-07-01_AT_x.pdf"
    result = redact_vin(path)
    assert "5YJ3E7EB9KF000316" not in result
    assert "…0316" in result


def test_leaves_text_without_vin_untouched():
    assert redact_vin("nothing to hide here 1234") == "nothing to hide here 1234"
    # 17-char runs containing I/O/Q are not VINs
    assert redact_vin("OIQOIQOIQOIQOIQOI") == "OIQOIQOIQOIQOIQOI"


def test_does_not_match_longer_runs():
    # An 18-character run is not a VIN; no partial match inside it.
    text = "A" * 18
    assert redact_vin(text) == text


def test_accepts_non_string_input():
    from pathlib import Path

    path = Path("tesla_charging_invoice_5YJ3E7EB9KF000316_2026-07-01_AT_x.pdf")
    assert "…0316" in redact_vin(path)
