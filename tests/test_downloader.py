import json
import os
from datetime import datetime

import pytest

from app.downloader import InvoiceDownloader
from tests.helpers import make_config


class TestParseCurrencyAmount:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1,234.56", 1234.56),
            ("1.234,56", 1234.56),
            ("-1.234,56", -1234.56),
            ("8,50", 8.5),
            ("9.99", 9.99),
            # A single separator followed by exactly three digits is a
            # thousands separator (invoices print decimals with 1-2 digits)
            ("1,234", 1234.0),
            ("1.234", 1234.0),
            # ... unless the integer part is a bare zero
            ("0,375", 0.375),
            ("0.375", 0.375),
            ("1,234,567", 1234567.0),
            ("1.234.567", 1234567.0),
            ("", 0.0),
            ("abc", 0.0),
        ],
    )
    def test_parse(self, text, expected):
        assert InvoiceDownloader._parse_currency_amount(text) == expected


class TestIsDesiredDate:
    def test_none_means_all(self):
        assert InvoiceDownloader._is_desired_date(datetime(2020, 5, 17), None)

    def test_matching_month(self):
        months = InvoiceDownloader._month_set([datetime(2026, 7, 1), datetime(2026, 6, 1)])
        assert InvoiceDownloader._is_desired_date(datetime(2026, 7, 15), months)
        assert InvoiceDownloader._is_desired_date(datetime(2026, 6, 30), months)
        assert not InvoiceDownloader._is_desired_date(datetime(2026, 5, 31), months)
        assert not InvoiceDownloader._is_desired_date(datetime(2025, 7, 15), months)


class TestWriteMetadata:
    def test_creates_file(self, tmp_path):
        path = tmp_path / "a.json"
        InvoiceDownloader._write_metadata(path, {"type": "charging"})
        assert json.loads(path.read_text()) == {"type": "charging"}


class TestDownloadInvoices:
    def test_accepts_old_rest_charging_history_payload(self, tmp_path):
        config = make_config(tmp_path, enable_subscription_invoice=False)

        class DummyClient:
            def get_vehicles(self):
                return {"VIN123": {"display_name": "My Tesla"}}

            def get_charging_history(self, vin):
                # old REST shape: {"data": [session, ...]}
                return {
                    "data": [
                        {
                            "unlatchDateTime": "2024-02-20T10:00:00",
                            "countryCode": "US",
                            "invoices": [{"contentId": "invoice-1", "fileName": "charging.pdf"}],
                        }
                    ]
                }

            def get_charging_invoice(self, invoice_id, vin):
                return b"%PDF-1.4"

        downloader = InvoiceDownloader(config, DummyClient())
        downloader.download_invoices()

        assert (config.invoice_path / "tesla_charging_invoice_VIN123_2024-02-20_US_charging.pdf").exists()

    def test_graphql_charging_history_sessions_and_fee_metadata(self, tmp_path):
        config = make_config(tmp_path, enable_subscription_invoice=False)

        session = {
            "countryCode": "DE",
            "siteLocationName": "Example City, Germany",
            "chargeStartDateTime": "2025-08-02T12:00:00+02:00",
            "chargeStopDateTime": "2025-08-02T12:20:00+02:00",
            "unlatchDateTime": "2025-08-02T12:20:05+02:00",
            "invoices": [
                {
                    "fileName": "INV2025000001_DE-DE.pdf",
                    "contentId": "content-1",
                    "invoiceType": "IMMEDIATE",
                }
            ],
            "fees": [
                {
                    "feeType": "CHARGING",
                    "currencyCode": "EUR",
                    "usageBase": 20.5,
                    "usageTier1": 7,
                    "usageTier2": 12,
                    "totalBase": 6.15,
                    "totalDue": 6.15,
                },
                {
                    "feeType": "PARKING",
                    "currencyCode": "EUR",
                    "usageBase": 0,
                    "totalBase": 0,
                    "totalDue": 0.5,
                },
            ],
        }

        class DummyClient:
            def get_vehicles(self):
                return {"VIN123": {"display_name": "My Tesla"}}

            def get_charging_history(self, vin):
                # The API client returns the flattened session list
                return [session]

            def get_charging_invoice(self, invoice_id, vin):
                return b"%PDF-1.4"

        downloader = InvoiceDownloader(config, DummyClient())
        downloader.download_invoices()

        pdf = config.invoice_path / "tesla_charging_invoice_VIN123_2025-08-02_DE_INV2025000001_DE-DE.pdf"
        assert pdf.exists()
        meta = json.loads(pdf.with_suffix(".json").read_text())
        assert meta["energy_kwh"] == 20.5
        assert meta["total_tier1_kwh"] == 7
        assert meta["total_tier2_kwh"] == 12
        assert meta["total_base_cost"] == 6.15
        assert meta["total_cost"] == 6.65  # charging + parking fee
        assert meta["currency"] == "EUR"
        assert meta["site_name"] == "Example City, Germany"

    def test_sessions_from_other_vehicles_are_ignored(self, tmp_path):
        # Defense against the GraphQL endpoint returning account-wide history
        # despite the vin filter: another vehicle's session must neither be
        # downloaded nor saved under this VIN's file name.
        config = make_config(tmp_path, enable_subscription_invoice=False)

        def session(vin, day, content_id, filename):
            return {
                "vin": vin,
                "unlatchDateTime": f"2024-02-{day}T10:00:00",
                "countryCode": "US",
                "invoices": [{"contentId": content_id, "fileName": filename}],
            }

        class DummyClient:
            def get_vehicles(self):
                return {"VIN123": {"display_name": "My Tesla"}}

            def get_charging_history(self, vin):
                return [
                    session("OTHERVIN", "20", "foreign", "foreign.pdf"),
                    session("VIN123", "21", "own", "own.pdf"),
                ]

            def get_charging_invoice(self, invoice_id, vin):
                assert invoice_id == "own", "another vehicle's invoice must not be downloaded"
                return b"%PDF-1.4"

        InvoiceDownloader(config, DummyClient()).download_invoices()

        assert (config.invoice_path / "tesla_charging_invoice_VIN123_2024-02-21_US_own.pdf").exists()
        assert not list(config.invoice_path.glob("*foreign*"))

    def test_extracts_sessions_from_raw_graphql_payload(self):
        payload = {
            "data": {
                "me": {
                    "charging": {
                        "historyV2": {
                            "data": [{"chargeSessionId": "abc"}],
                            "totalResults": 1,
                            "hasMoreData": False,
                            "pageNumber": 1,
                        }
                    }
                }
            }
        }
        assert InvoiceDownloader._extract_charging_sessions(payload) == [{"chargeSessionId": "abc"}]

    def test_extract_cost_from_pdf_supports_euro_amounts(self, monkeypatch):
        class DummyPage:
            def extract_text(self):
                return "Total due: EUR 1.234,56"

        class DummyReader:
            def __init__(self, *_args, **_kwargs):
                self.pages = [DummyPage()]

        import sys
        import types

        fake_pypdf = types.SimpleNamespace(PdfReader=lambda *_args, **_kwargs: DummyReader())
        monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

        amount, currency = InvoiceDownloader.extract_cost_from_pdf(b"fake")

        assert amount == 1234.56
        assert currency == "EUR"

    def test_extract_cost_from_pdf_supports_symbol_currency(self, monkeypatch):
        class DummyPage:
            def extract_text(self):
                return "Amount due: US$ 42.00"

        class DummyReader:
            def __init__(self, *_args, **_kwargs):
                self.pages = [DummyPage()]

        import sys
        import types

        fake_pypdf = types.SimpleNamespace(PdfReader=lambda *_args, **_kwargs: DummyReader())
        monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

        amount, currency = InvoiceDownloader.extract_cost_from_pdf(b"fake")

        assert amount == 42.0
        assert currency == "USD"

    def test_extract_cost_from_pdf_german_subscription_invoice(self, monkeypatch):
        # Real layout of a Tesla subscription invoice (AT): the grand total is
        # on the "Gesamtbetrag (EUR)" line; the tax rate (20.00) must not win.
        class DummyPage:
            def extract_text(self):
                return (
                    "Beschreibung Preis/Einheit (EUR) Anzahl Steuern (%) Total (EUR)\n"
                    "Premium-Konnektivität 8.32 1 20 8.32\n"
                    "Teilsumme 8.32\n"
                    "Gesamtsumme Steuern 1.67\n"
                    "Gesamtbetrag (EUR) 9.99\n"
                    "Code Gesamtbetrag (EUR) Steuern (%) Gesamtsumme Steuern (EUR) Beschreibung\n"
                    "ATSR 8.32 20.00 1.67 Örtlicher MwSt. Standardsatz"
                )

        class DummyReader:
            def __init__(self, *_args, **_kwargs):
                self.pages = [DummyPage()]

        import sys
        import types

        fake_pypdf = types.SimpleNamespace(PdfReader=lambda *_args, **_kwargs: DummyReader())
        monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

        amount, currency = InvoiceDownloader.extract_cost_from_pdf(b"fake")

        assert amount == 9.99
        assert currency == "EUR"

    def test_extract_cost_keeps_negative_credit_note_totals(self, monkeypatch):
        class DummyPage:
            def extract_text(self):
                return "Total due: EUR -1.234,56"

        class DummyReader:
            def __init__(self, *_args, **_kwargs):
                self.pages = [DummyPage()]

        import sys
        import types

        fake_pypdf = types.SimpleNamespace(PdfReader=lambda *_args, **_kwargs: DummyReader())
        monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

        amount, currency = InvoiceDownloader.extract_cost_from_pdf(b"fake")

        assert amount == -1234.56
        assert currency == "EUR"

    def test_extract_cost_fallback_ignores_vat_as_currency(self, monkeypatch):
        # No recognizable total line: the fallback must not pick "VAT 20.00"
        # (larger number) over the real "8.00 EUR" amount.
        class DummyPage:
            def extract_text(self):
                return "Something 8.00 EUR\nVAT 20.00"

        class DummyReader:
            def __init__(self, *_args, **_kwargs):
                self.pages = [DummyPage()]

        import sys
        import types

        fake_pypdf = types.SimpleNamespace(PdfReader=lambda *_args, **_kwargs: DummyReader())
        monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

        amount, currency = InvoiceDownloader.extract_cost_from_pdf(b"fake")

        assert amount == 8.0
        assert currency == "EUR"

    def test_one_failing_invoice_does_not_abort_sync(self, tmp_path):
        config = make_config(tmp_path, enable_subscription_invoice=False)

        def session(content_id, filename):
            return {
                "unlatchDateTime": "2024-02-20T10:00:00",
                "countryCode": "US",
                "invoices": [{"contentId": content_id, "fileName": filename}],
            }

        class DummyClient:
            def get_vehicles(self):
                return {"VIN123": {"display_name": "My Tesla"}}

            def get_charging_history(self, vin):
                return [session("bad", "bad.pdf"), session("good", "good.pdf")]

            def get_charging_invoice(self, invoice_id, vin):
                if invoice_id == "bad":
                    raise RuntimeError("boom")
                return b"%PDF-1.4"

        InvoiceDownloader(config, DummyClient()).download_invoices()

        assert (config.invoice_path / "tesla_charging_invoice_VIN123_2024-02-20_US_good.pdf").exists()
        assert not (config.invoice_path / "tesla_charging_invoice_VIN123_2024-02-20_US_bad.pdf").exists()

    def test_merge_preserves_foreign_keys(self, tmp_path):
        # The email exporter's sent-flag must survive metadata refreshes,
        # otherwise every poll cycle would trigger duplicate emails.
        path = tmp_path / "a.json"
        path.write_text(json.dumps({"email_sent": 1234, "total_cost": 1}))
        InvoiceDownloader._write_metadata(path, {"total_cost": 2, "type": "charging"})

        result = json.loads(path.read_text())
        assert result == {"email_sent": 1234, "total_cost": 2, "type": "charging"}

    def test_unchanged_content_not_rewritten(self, tmp_path):
        path = tmp_path / "a.json"
        InvoiceDownloader._write_metadata(path, {"type": "charging", "total_cost": 5})
        mtime = path.stat().st_mtime_ns
        InvoiceDownloader._write_metadata(path, {"type": "charging", "total_cost": 5})
        assert path.stat().st_mtime_ns == mtime

    def test_corrupt_existing_file_is_replaced(self, tmp_path):
        path = tmp_path / "a.json"
        path.write_text("{broken")
        InvoiceDownloader._write_metadata(path, {"type": "charging"})
        assert json.loads(path.read_text()) == {"type": "charging"}


def _subscription_client(invoices):
    class DummyClient:
        def get_vehicles(self):
            return {"VIN123": {"display_name": "My Tesla"}}

        def get_charging_history(self, vin):
            return []

        def get_subscription_invoices(self, vin):
            return {"data": invoices}

        def get_subscription_invoice(self, invoice_id, vin):
            return b"%PDF-1.4"

    return DummyClient()


class TestSubscriptionInvoices:
    def test_credit_note_cost_is_negative(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        invoice = {
            "InvoiceDate": "2026-05-01T00:00:00",
            "InvoiceId": "INV-1",
            "InvoiceFileName": "invoice.pdf",
            "IsCreditNote": True,
        }
        # Some credit-note layouts print the refunded amount as positive
        monkeypatch.setattr(InvoiceDownloader, "extract_cost_from_pdf", staticmethod(lambda _b: (9.99, "EUR")))

        InvoiceDownloader(config, _subscription_client([invoice])).download_invoices()

        pdf = config.invoice_path / "tesla_subscription_invoice_VIN123_2026-05-01_creditnote.pdf"
        assert pdf.exists()
        meta = json.loads(pdf.with_suffix(".json").read_text())
        assert meta["total_cost"] == -9.99
        assert meta["is_credit_note"] is True
        assert meta["invoice_id"] == "INV-1"

    def test_failed_extraction_is_not_reparsed_every_cycle(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        invoice = {"InvoiceDate": "2026-05-01T00:00:00", "InvoiceId": "INV-1", "InvoiceFileName": "invoice.pdf"}
        calls = []

        def fake_extract(_b):
            calls.append(1)
            return 0.0, ""

        monkeypatch.setattr(InvoiceDownloader, "extract_cost_from_pdf", staticmethod(fake_extract))
        downloader = InvoiceDownloader(config, _subscription_client([invoice]))

        downloader.download_invoices()
        downloader.download_invoices()

        # Parsed once; the stored current-version result (even 0.0) is reused
        assert len(calls) == 1

    def test_same_day_invoices_get_distinct_files(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        invoices = [
            {"InvoiceDate": "2026-05-01T00:00:00", "InvoiceId": "INV-A", "InvoiceFileName": "a.pdf"},
            {"InvoiceDate": "2026-05-01T00:00:00", "InvoiceId": "INV-B", "InvoiceFileName": "b.pdf"},
        ]
        monkeypatch.setattr(InvoiceDownloader, "extract_cost_from_pdf", staticmethod(lambda _b: (9.99, "EUR")))

        InvoiceDownloader(config, _subscription_client(invoices)).download_invoices()

        plain = config.invoice_path / "tesla_subscription_invoice_VIN123_2026-05-01.pdf"
        suffixed = config.invoice_path / "tesla_subscription_invoice_VIN123_2026-05-01_INV-B.pdf"
        assert plain.exists()
        assert suffixed.exists()
        assert json.loads(plain.with_suffix(".json").read_text())["invoice_id"] == "INV-A"
        assert json.loads(suffixed.with_suffix(".json").read_text())["invoice_id"] == "INV-B"


class TestLocalTimezone:
    def test_aware_dates_use_local_month(self):
        # An invoice at 23:00 UTC on June 30 belongs to July in Vienna —
        # month bucketing and file names must follow the local calendar.
        import os
        import time

        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Vienna"
        time.tzset()
        try:
            local = InvoiceDownloader._as_local(datetime.fromisoformat("2026-06-30T23:00:00+00:00"))
            assert (local.year, local.month, local.day) == (2026, 7, 1)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

    def test_naive_dates_stay_untouched(self):
        # Date-only subscription invoices are already calendar dates;
        # attaching a zone could shift them and break the UI's
        # midnight-means-date-only display rule.
        local = InvoiceDownloader._as_local(datetime(2026, 7, 1))
        assert local == datetime(2026, 7, 1)
        assert local.tzinfo is None


class TestChargingInvoiceSwitch:
    def test_charging_disabled_skips_charging_history(self, tmp_path):
        config = make_config(tmp_path, enable_charging_invoice=False)

        class DummyClient:
            def get_vehicles(self):
                return {"VIN123": {"display_name": "My Tesla"}}

            def get_charging_history(self, vin):
                raise AssertionError("charging history must not be fetched when disabled")

            def get_subscription_invoices(self, vin):
                return {"data": []}

        InvoiceDownloader(config, DummyClient()).download_invoices()
        assert not list(config.invoice_path.glob("*charging*"))

    def test_both_disabled_only_checks_the_connection(self, tmp_path):
        # Both switches off is a legal "downloads paused" state: the sync
        # still fetches the vehicle list (token/connection check) but must
        # not touch any invoice endpoint.
        config = make_config(tmp_path, enable_charging_invoice=False, enable_subscription_invoice=False)
        vehicle_calls = []

        class DummyClient:
            def get_vehicles(self):
                vehicle_calls.append(True)
                return {"VIN123": {"display_name": "My Tesla"}}

            def get_charging_history(self, vin):
                raise AssertionError("charging history must not be fetched when disabled")

            def get_subscription_invoices(self, vin):
                raise AssertionError("subscription invoices must not be fetched when disabled")

        InvoiceDownloader(config, DummyClient()).download_invoices()
        assert vehicle_calls  # the connection check still ran
        assert not list(config.invoice_path.glob("*.pdf"))


class TestExtractSessionsRobustness:
    def test_non_dict_levels_do_not_crash(self):
        # Tesla answering "me": null / a plain string must yield an empty
        # list, not an AttributeError that kills the whole sync.
        assert InvoiceDownloader._extract_charging_sessions({"data": {"me": None}}) == []
        assert InvoiceDownloader._extract_charging_sessions({"data": {"me": "Not Found"}}) == []
        assert InvoiceDownloader._extract_charging_sessions({"data": {"me": {"charging": 42}}}) == []


def test_charging_invoice_named_xml_is_stored_as_pdf(tmp_path):
    # Tesla names some old charging invoices INVOICESTATUS….XML; they are PDFs.
    config = make_config(tmp_path, enable_subscription_invoice=False)

    class DummyClient:
        def get_vehicles(self):
            return {"VIN123": {"display_name": "My Tesla"}}

        def get_charging_history(self, vin):
            return [
                {
                    "vin": "VIN123",
                    "unlatchDateTime": "2022-06-11T10:00:00",
                    "countryCode": "PT",
                    "invoices": [{"contentId": "c1", "fileName": "INVOICESTATUS2036355778037.XML"}],
                }
            ]

        def get_charging_invoice(self, invoice_id, vin):
            return b"%PDF-1.4"

    InvoiceDownloader(config, DummyClient()).download_invoices()

    pdf = config.invoice_path / "tesla_charging_invoice_VIN123_2022-06-11_PT_INVOICESTATUS2036355778037.pdf"
    assert pdf.read_bytes() == b"%PDF-1.4"
    assert json.loads(pdf.with_suffix(".json").read_text())["filename"] == pdf.name


def test_nameless_charging_invoice_is_stored_as_pdf_and_old_files_move(tmp_path):
    # Old sessions list their invoice with an empty fileName; it used to be
    # stored as a file ending in "_" (no .pdf), with a sidecar "…_.json".
    config = make_config(tmp_path, enable_subscription_invoice=False)
    config.invoice_path.mkdir(parents=True)
    old = config.invoice_path / "tesla_charging_invoice_VIN123_2021-06-08_PT_"
    old.write_bytes(b"%PDF-old")
    (config.invoice_path / "tesla_charging_invoice_VIN123_2021-06-08_PT_.json").write_text('{"email_sent": "x"}')
    fetched = []

    class DummyClient:
        def get_vehicles(self):
            return {"VIN123": {"display_name": "My Tesla"}}

        def get_charging_history(self, vin):
            return [
                {
                    "vin": "VIN123",
                    "unlatchDateTime": "2021-06-08T10:00:00",
                    "countryCode": "PT",
                    "invoices": [{"contentId": "abc-123", "fileName": ""}],
                }
            ]

        def get_charging_invoice(self, invoice_id, vin):
            fetched.append(invoice_id)
            return b"%PDF-new"

    InvoiceDownloader(config, DummyClient()).download_invoices()

    pdf = config.invoice_path / "tesla_charging_invoice_VIN123_2021-06-08_PT_abc-123.pdf"
    assert pdf.read_bytes() == b"%PDF-old"  # moved, not downloaded again
    assert fetched == []
    meta = json.loads(pdf.with_suffix(".json").read_text())
    assert meta["email_sent"] == "x" and meta["filename"] == pdf.name  # the sidecar moved with it
    assert sorted(p.name for p in config.invoice_path.iterdir()) == [pdf.with_suffix(".json").name, pdf.name]



class TestInvoiceDateFromPdf:
    @pytest.mark.parametrize(
        ("text", "currency", "expected"),
        [
            # PT: a table, the label heads a column, values follow the other headers
            ("Data da fatura\n Cliente\n NIF\n514063858\n FT VEH999/53776\n 06/07/2023\n", "EUR", (2023, 7, 6)),
            ("Rechnungsdatum: 06.07.2023", "EUR", (2023, 7, 6)),
            ("Invoice Date 07/06/2023", "USD", (2023, 7, 6)),  # US prints month/day
            ("Invoice Date 13/06/2023", "USD", (2023, 6, 13)),  # unambiguous wins over currency
            ("Invoice date 2023-07-06", "GBP", (2023, 7, 6)),
            ("Fecha de factura 6-7-23", "EUR", (2023, 7, 6)),
            ("Total 8,12 EUR", "EUR", None),
            ("Invoice Date 31/02/2023", "EUR", None),
        ],
    )
    def test_extract_date(self, monkeypatch, text, currency, expected):
        monkeypatch.setattr(InvoiceDownloader, "_pdf_text", staticmethod(lambda _b: text))
        result = InvoiceDownloader.extract_date_from_pdf(b"%PDF", currency)
        assert (result and (result.year, result.month, result.day)) == (expected or None)

    def test_placeholder_dated_invoice_gets_the_pdf_date(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        invoice = {"InvoiceDate": "0001-01-01T00:00:00", "InvoiceId": "INV-1", "InvoiceFileName": "i.pdf"}
        monkeypatch.setattr(InvoiceDownloader, "extract_cost_from_pdf", staticmethod(lambda _b: (8.12, "EUR")))
        reads = []

        def pdf_date(_b, _currency):
            reads.append(1)
            return datetime(2023, 7, 6)

        monkeypatch.setattr(InvoiceDownloader, "extract_date_from_pdf", staticmethod(pdf_date))
        downloader = InvoiceDownloader(config, _subscription_client([invoice]))
        downloader.download_invoices()
        downloader.download_invoices()  # the second sync reuses the stored date

        sidecar = config.invoice_path / "tesla_subscription_invoice_VIN123_0001-01-01.json"
        assert json.loads(sidecar.read_text())["date"] == "2023-07-06T00:00:00"
        assert reads == [1]


def test_charging_invoices_from_other_networks_are_downloaded(tmp_path):
    # IONITY sessions billed by Tesla carry no VIN: the per-vehicle history
    # never returns them, only the account-wide one does.
    config = make_config(tmp_path, enable_subscription_invoice=False)
    own = {
        "vin": "VIN123",
        "unlatchDateTime": "2026-09-01T10:00:00",
        "countryCode": "PT",
        "invoices": [{"contentId": "own", "fileName": "own.pdf"}],
    }
    ionity = {
        "vin": None,
        "operator": "IONITY",
        "programType": "EXTERNAL",
        "siteLocationName": "IONITY Obidos",
        "unlatchDateTime": "2026-09-08T10:00:00",
        "countryCode": "PT",
        "fees": [{"feeType": "CHARGING", "totalDue": 11.43, "currencyCode": "EUR", "uom": "kWh", "usageBase": 16.592}],
        "invoices": [{"contentId": "ion", "fileName": "4036MSP0000000419PT-PT.pdf"}],
    }
    fetched = []

    class DummyClient:
        def get_vehicles(self):
            return {"VIN123": {"display_name": "My Tesla"}}

        def get_charging_history(self, vin):
            return [own] if vin else [own, ionity]

        def get_charging_invoice(self, invoice_id, vin):
            fetched.append(invoice_id)
            return b"%PDF-1.4"

    InvoiceDownloader(config, DummyClient()).download_invoices()

    assert sorted(fetched) == ["ion", "own"]  # the own session is not fetched twice
    pdf = config.invoice_path / "tesla_charging_invoice_IONITY_2026-09-08_PT_4036MSP0000000419PT-PT.pdf"
    meta = json.loads(pdf.with_suffix(".json").read_text())
    assert meta["site_name"] == "IONITY Obidos"
    assert meta["total_cost"] == 11.43
    assert meta["vin"] == ""


class TestAccountOwnership:
    """Several accounts share one invoice folder; the first to save an invoice keeps it."""

    @staticmethod
    def _charging_client(fetched):
        class DummyClient:
            def get_vehicles(self):
                return {"VIN123": {"display_name": "Her name for it"}}

            def get_charging_history(self, vin):
                return [
                    {
                        "vin": "VIN123",
                        "unlatchDateTime": "2024-02-21T10:00:00",
                        "countryCode": "PT",
                        "invoices": [{"contentId": "c1", "fileName": "one.pdf"}],
                    }
                ]

            def get_charging_invoice(self, invoice_id, vin):
                fetched.append(invoice_id)
                return b"%PDF-1.4"

        return DummyClient()

    def _sidecar(self, config):
        return config.invoice_path / "tesla_charging_invoice_VIN123_2024-02-21_PT_one.json"

    def test_new_sidecars_record_their_account(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, enable_subscription_invoice=False)
        InvoiceDownloader(config, self._charging_client([]), "a1").download_invoices()
        assert json.loads(self._sidecar(config).read_text())["account"] == "a1"

        sub_config = make_config(tmp_path / "s")
        monkeypatch.setattr(InvoiceDownloader, "extract_cost_from_pdf", staticmethod(lambda _b: (1.0, "EUR")))
        invoice = {"InvoiceDate": "2026-05-01T00:00:00", "InvoiceId": "INV-1", "InvoiceFileName": "i.pdf"}
        InvoiceDownloader(sub_config, _subscription_client([invoice]), "a1").download_invoices()
        meta = json.loads((sub_config.invoice_path / "tesla_subscription_invoice_VIN123_2026-05-01.json").read_text())
        assert meta["account"] == "a1"

    def test_another_accounts_invoice_is_left_alone(self, tmp_path):
        config = make_config(tmp_path, enable_subscription_invoice=False)
        config.invoice_path.mkdir(parents=True)
        sidecar = self._sidecar(config)
        sidecar.write_text(json.dumps({"account": "a1", "vehicle_name": "His name for it"}))
        before = sidecar.read_bytes()
        fetched = []

        InvoiceDownloader(config, self._charging_client(fetched), "a2").download_invoices()

        assert sidecar.read_bytes() == before
        assert fetched == []

    def test_pre_multi_account_sidecars_stay_with_the_default_account(self, tmp_path):
        config = make_config(tmp_path, enable_subscription_invoice=False)
        config.invoice_path.mkdir(parents=True)
        sidecar = self._sidecar(config)
        sidecar.write_text(json.dumps({"vehicle_name": "Old"}))
        sidecar.with_suffix(".pdf").write_bytes(b"%PDF-1.4")

        InvoiceDownloader(config, self._charging_client([]), "a2").download_invoices()
        assert json.loads(sidecar.read_text()) == {"vehicle_name": "Old"}

        InvoiceDownloader(config, self._charging_client([])).download_invoices()
        meta = json.loads(sidecar.read_text())
        assert meta["vehicle_name"] == "Her name for it"
        assert "account" not in meta  # still the default's by owner_of(), never stamped


def test_adopt_legacy_never_deletes_the_file_it_keeps(tmp_path):
    # Two names for one file (here a hard link; on a case-insensitive
    # filesystem "X.PDF" and "X.pdf"): the old name must not be unlinked
    # as a duplicate, or the invoice is lost and re-downloaded every sync.
    new = tmp_path / "invoice.pdf"
    new.write_bytes(b"%PDF-1.4")
    old = tmp_path / "invoice_legacy"
    os.link(new, old)

    InvoiceDownloader._adopt_legacy(old, new)

    assert old.exists()  # never unlinked: it is not a separate copy
    assert new.read_bytes() == b"%PDF-1.4"


def test_adopt_legacy_fixes_the_case_on_case_insensitive_filesystems(tmp_path):
    old = tmp_path / "invoice.PDF"
    old.write_bytes(b"%PDF-1.4")
    new = tmp_path / "invoice.pdf"
    if not new.exists():
        pytest.skip("case-sensitive filesystem")

    InvoiceDownloader._adopt_legacy(old, new)
    InvoiceDownloader._adopt_legacy(old, new)  # the next sync: nothing to do

    assert os.listdir(tmp_path) == ["invoice.pdf"]
    assert new.read_bytes() == b"%PDF-1.4"
