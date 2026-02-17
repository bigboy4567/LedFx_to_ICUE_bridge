import locale
import os


_MESSAGES = {
    "fr": {
        "window_title": "Choisir mode",
        "app_title": "LedFx iCUE Bridge",
        "choose_mode": "Choisir le mode (defaut: {default_mode})",
        "mode_selected": "Mode selectionne: {mode}",
        "mode_unique": "Unique",
        "mode_group": "Groupe",
        "mode_fusion": "Fusion",
        "quit": "Quitter",
        "confirm_close_title": "Confirmer la fermeture",
        "confirm_close_body": "Voulez-vous fermer le programme ?",
        "update_available_title": "Mise a jour disponible",
        "update_available_body": "Nouvelle version disponible ({version}). Telecharger maintenant ?",
        "update_available_console": "Nouvelle version disponible: {version} -> {url}",
    },
    "en": {
        "window_title": "Choose mode",
        "app_title": "LedFx iCUE Bridge",
        "choose_mode": "Choose the mode (default: {default_mode})",
        "mode_selected": "Selected mode: {mode}",
        "mode_unique": "Unique",
        "mode_group": "Group",
        "mode_fusion": "Fusion",
        "quit": "Quit",
        "confirm_close_title": "Confirm exit",
        "confirm_close_body": "Do you want to close the program?",
        "update_available_title": "Update available",
        "update_available_body": "A new version is available ({version}). Download now?",
        "update_available_console": "Update available: {version} -> {url}",
    },
}


def _normalize_lang(value):
    if not value:
        return None
    v = str(value).strip().lower()
    if v.startswith("fr"):
        return "fr"
    if v.startswith("en"):
        return "en"
    return None


def _detect_lang():
    env_lang = _normalize_lang(os.environ.get("LEDFX_UI_LANG"))
    if env_lang:
        return env_lang
    try:
        loc = locale.getlocale()[0] or ""
    except Exception:
        loc = ""
    detected = _normalize_lang(loc)
    return detected or "fr"


class I18n:
    def __init__(self, lang=None):
        self.lang = _normalize_lang(lang) or _detect_lang()

    def t(self, key, **kwargs):
        msg = _MESSAGES.get(self.lang, _MESSAGES["fr"]).get(key, key)
        if kwargs:
            try:
                return msg.format(**kwargs)
            except Exception:
                return msg
        return msg


def get_i18n(cfg=None, lang=None):
    if lang is None and cfg is not None:
        lang = cfg.get("ui_language")
    return I18n(lang)
