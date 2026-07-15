"""Consistency checks for the dashboard translation files.

Translations are contributed as plain JSON files in app/static/i18n/ (see
README, "Contributing a translation"); these tests catch the typical drift:
a language missing keys, broken {placeholders}, or markup/scripts using a
key that no language defines.
"""

import json
import re
from pathlib import Path

STATIC_DIR = Path(__file__).parent.parent / "app" / "static"
I18N_DIR = STATIC_DIR / "i18n"


def _languages() -> dict:
    return json.loads((I18N_DIR / "languages.json").read_text())


def _translations(code: str) -> dict:
    return json.loads((I18N_DIR / f"{code}.json").read_text())


def test_manifest_lists_english_and_every_language_has_a_file():
    languages = _languages()
    assert "en" in languages, "English is the fallback and must always exist"
    for code, name in languages.items():
        assert re.fullmatch(r"[a-z]{2}", code), f"language code {code!r} must be two lowercase letters"
        assert name.strip(), f"language {code} needs a native name"
        assert (I18N_DIR / f"{code}.json").is_file(), f"missing translation file for {code}"


def test_all_languages_cover_the_same_keys():
    en_keys = set(_translations("en"))
    for code in _languages():
        keys = set(_translations(code))
        assert keys == en_keys, (
            f"{code}.json is out of sync with en.json: "
            f"missing {sorted(en_keys - keys)}, extra {sorted(keys - en_keys)}"
        )


def test_placeholders_match_english():
    def placeholders(text: str) -> set:
        return set(re.findall(r"\{(\w+)\}", text))

    en = _translations("en")
    for code in _languages():
        for key, text in _translations(code).items():
            assert placeholders(text) == placeholders(en[key]), (
                f"{code}.json key {key!r} must keep the same {{placeholders}} as en.json"
            )


def test_markup_only_uses_known_keys():
    en = _translations("en")
    html = (STATIC_DIR / "index.html").read_text()
    keys = re.findall(r'data-i18n(?:-placeholder)?="([^"]+)"', html)
    assert keys, "index.html should carry data-i18n attributes"
    missing = sorted({k for k in keys if k not in en})
    assert not missing, f"index.html references translation keys missing from en.json: {missing}"


def test_scripts_only_use_known_keys():
    en = _translations("en")
    js = (STATIC_DIR / "js" / "app.js").read_text()
    # t('key') / t("key") calls; dynamic prefixes like t('type_' + …) end
    # with an underscore and are skipped (their expansions are checked via
    # the markup/other literal uses).
    keys = re.findall(r"""\bt\(\s*['"]([a-z0-9_]+)['"]""", js)
    assert keys, "app.js should call t()"
    missing = sorted({k for k in keys if not k.endswith("_") and k not in en})
    assert not missing, f"app.js uses translation keys missing from en.json: {missing}"
