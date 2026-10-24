"""Title normalisation and matching, shared by search ranking, discover matching
and the Suwayomi source picker. Rules carried over from Comic Requests v1.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

# Non-English reprint publishers (penalised) and English-language publishers (boosted).
FOREIGN = re.compile(
    r"panini|urban comics|\becc\b|ediciones|planeta|deagostini|\bdino\b|egmont|semic|gl[eé]nat|"
    r"\bkana\b|carlsen|televisa|mondadori|rw edizioni|planet manga|\bpika\b|kaz[eé]|\bnorma\b|"
    r"ivrea|\bjbc\b|hachette|delcourt|soleil|dargaud|altraverse|star comics|j-pop|edizioni|"
    r"editora|editorial|verlag|edi[cç][oõ]es|levoir|mythos|abril|salvat|eaglemoss|"
    r"bao publishing|saldapress",
    re.I,
)
ENGLISH = re.compile(
    r"^(marvel|dc comics|image|dark horse|idw|boom|viz|kodansha|yen press|seven seas|dynamite|"
    r"oni|skybound|fantagraphics|drawn|first second|scholastic|graphix|abrams|webtoon|ize press|"
    r"vertigo|valiant|titan|archie|mad cave|vault|ahoy|aftershock|top shelf|humanoids|ablaze|"
    r"tokyopop|square enix|udon|vertical|denpa|wildstorm|black label|harper|random house|"
    r"little, brown|simon|macmillan|andrews mcmeel|iron circus|silver sprocket|tinto)",
    re.I,
)
# Editions a manga source may list that are not the series itself.
SKIP_EDITIONS = (
    "doujinshi",
    "anthology",
    "fan colored",
    "fan-colored",
    "fancolor",
    "book version",
    "novel",
)

_VOL = re.compile(r"\b(vol(ume)?\.?|book|part|tp|tpb|hc|omnibus)\s*#?\d+\b", re.I)
_PARENS = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_YEAR = re.compile(r"\((\d{4})\)")
_LEADING_NUM = re.compile(r"^\s*#?\d+[\)\.:]\s*")


def fold(text: str) -> str:
    """Lower-case and strip accents."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def norm(text: str) -> str:
    """Comparable form: no accents, volume numbers, bracketed notes, punctuation or 'the'."""
    t = fold(text)
    t = _PARENS.sub(" ", t)
    t = _VOL.sub(" ", t)
    t = t.replace("&", " and ")
    t = _NON_WORD.sub(" ", t).strip()
    if t.startswith("the "):
        t = t[4:]
    return " ".join(t.split())


def similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def best_similarity(candidate: str, wanted: list[str]) -> float:
    return max((similarity(candidate, w) for w in wanted if w), default=0.0)


def is_latin(text: str | None) -> bool:
    if not text:
        return False
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) / len(letters) > 0.8


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = norm(item)
        if item and key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def year_hint(text: str) -> int | None:
    m = _YEAR.search(text or "")
    return int(m.group(1)) if m else None


def clean_list_title(raw: str) -> str:
    """'#12) Saga Vol. 1 (2012)' -> 'Saga Vol. 1'."""
    t = _LEADING_NUM.sub("", raw or "").strip()
    t = _YEAR.sub("", t).strip(" -–—:")
    return " ".join(t.split())


def comic_score(
    *,
    title: str,
    name: str,
    publisher: str | None,
    issues: int | None,
    year: str | None,
    hint: int | None,
    has_cover: bool,
    translated: bool = False,
    preferred: list[str] | None = None,
) -> tuple[float, float]:
    """(similarity, ranking score) for a ComicVine volume against a wanted title (v1 rules)."""
    sim = similarity(name, title)
    score = sim * 100
    pub = publisher or ""
    if translated or FOREIGN.search(pub):
        score -= 40
    elif ENGLISH.search(pub):
        score += 12
    if preferred and any(p and p.lower() in pub.lower() for p in preferred):
        score += 6
    score += min(int(issues or 0), 200) / 20
    if hint and (year or "").isdigit() and int(year) >= hint:
        score += 8
    if not has_cover:
        score -= 5
    return sim, score


def edition_penalty(title: str) -> float | None:
    """None if the source title is a skipped edition, else a small penalty for variants."""
    low = (title or "").lower()
    if any(word in low for word in SKIP_EDITIONS):
        return None
    return 0.02 if ("(" in low or "[" in low) else 0.0
