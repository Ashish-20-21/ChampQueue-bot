import json, unicodedata

_MAP_TRANSLATIONS = None

def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().upper()

def load_map_translations(path="resources/map_translations.json"):
    global _MAP_TRANSLATIONS
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    table = {}
    for canonical, variants in raw.items():
        table[normalize(canonical)] = canonical
        for variant in variants:
            table[normalize(variant)] = canonical
    _MAP_TRANSLATIONS = table

def resolve_map_name(ocr_text: str) -> str | None:
    return _MAP_TRANSLATIONS.get(normalize(ocr_text))
