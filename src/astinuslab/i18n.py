from __future__ import annotations

from .i18n_data.ca import TRANSLATIONS as _CA_TRANSLATIONS
from .i18n_data.en import TRANSLATIONS as _EN_TRANSLATIONS
from .i18n_data.es import TRANSLATIONS as _ES_TRANSLATIONS
from .i18n_data.fr import TRANSLATIONS as _FR_TRANSLATIONS

SUPPORTED_LANGS = {"en", "es", "fr", "ca"}
DEFAULT_LANG = "en"
LANG_NAMES = {
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "ca": "Català",
}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": _EN_TRANSLATIONS,
    "es": _ES_TRANSLATIONS,
    "fr": _FR_TRANSLATIONS,
    "ca": _CA_TRANSLATIONS,
}


def get_translations(lang: str) -> dict:
    return TRANSLATIONS.get(lang, TRANSLATIONS[DEFAULT_LANG])
