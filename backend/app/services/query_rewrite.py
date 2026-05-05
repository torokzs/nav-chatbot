import re

SYNONYM_MAP: dict[str, str] = {
    "szja": "személyi jövedelemadó",
    "áfa": "általános forgalmi adó",
    "kata": "kisadózó vállalkozók tételes adója",
    "ekho": "egyszerűsített közteherviselési hozzájárulás",
    "tao": "társasági adó",
    "szocho": "szociális hozzájárulási adó",
    "tbj": "társadalombiztosítási járulék",
    "kiva": "kisvállalati adó",
    "eho": "egészségügyi hozzájárulás",
}

_NUMBERED_SPLIT_PATTERN = re.compile(r"(?:^|\n)\s*\d+[.)]\s+", re.MULTILINE)
_QUESTION_SPLIT_PATTERN = re.compile(r"\?+")


def rewrite_query(query: str) -> list[str]:
    normalized = " ".join(query.split())
    if not normalized:
        return []

    parts = _split_questions(query)
    rewritten = [_expand_synonyms(part) for part in parts]
    return rewritten or [_expand_synonyms(normalized)]


def _split_questions(query: str) -> list[str]:
    if _NUMBERED_SPLIT_PATTERN.search(query):
        numbered_parts = [
            cleaned
            for part in _NUMBERED_SPLIT_PATTERN.split(query)
            if (cleaned := _clean_question(part))
        ]
        if numbered_parts:
            return numbered_parts

    cleaned_parts = [
        cleaned_part
        for part in _QUESTION_SPLIT_PATTERN.split(query)
        if (cleaned_part := _clean_question(part))
    ]
    if cleaned_parts:
        return cleaned_parts

    fallback = _clean_question(query)
    return [fallback] if fallback else []


def _clean_question(value: str) -> str:
    return re.sub(r"^\d+[.)]\s*", "", " ".join(value.split())).strip(" ?")


def _expand_synonyms(query: str) -> str:
    expanded = query
    for short_form, full_form in SYNONYM_MAP.items():
        expanded = re.sub(
            rf"\b{re.escape(short_form)}\b",
            f"{short_form} ({full_form})",
            expanded,
            flags=re.IGNORECASE,
        )
    return expanded
