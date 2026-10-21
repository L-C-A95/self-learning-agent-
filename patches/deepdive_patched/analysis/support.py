"""Claim-to-excerpt support check.

The span-grounding validator in ``deepdive.analysis.grounding`` proves only that
the quoted excerpt really occurs in the fetched page. It never checks that the
claim *text* is actually supported by that excerpt, so a model is free to swap
the subject (``AlphaGo`` -> ``AlphaGo Zero``), inject a term that was never in
the source (``蒙特卡洛``), or turn a six-character heading into a conclusion —
and still be reported as "grounded".

This module adds the missing edge. A claim survives only if every proper noun
and number it mentions can be found in its own excerpt. Cross-language pairs
(Chinese claim over an English excerpt) are rejected by default because they
cannot be compared character-by-character, which is precisely the situation
where the model silently rewrites.

Toggle with the environment variable ``DEEPDIVE_ALLOW_CROSSLINGUAL=1`` if you
deliberately want translated claims to pass.
"""

from __future__ import annotations

import os
import re

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_CAP_WORD = re.compile(r"[A-Z][A-Za-z0-9\-]{1,}")
_SENTENCE_END = ".!?\n"

_CN_DIGITS = {
    "零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}
_CN_PERCENT = re.compile(r"百分之([零〇一二两三四五六七八九十百]+)")
_CN_DECIMAL = re.compile(r"([零〇一二两三四五六七八九十百]+)点([零〇一二三四五六七八九]+)")

ALLOW_CROSSLINGUAL = os.environ.get("DEEPDIVE_ALLOW_CROSSLINGUAL", "0") == "1"

# Bilingual glossary for the vocabulary this engine is actually used on. A
# textbook says 毕渥数, the model writes "Biot number"; that is not a new fact,
# and treating it as one deletes correct sentences. Only names that denote the
# same concept in both languages belong here — nothing evaluative.
_TERM_PAIRS = [
    ("fourier", "傅里叶"), ("biot", "毕渥"), ("nusselt", "努塞尔"),
    ("reynolds", "雷诺"), ("prandtl", "普朗特"), ("grashof", "格拉晓夫"),
    ("rayleigh", "瑞利"), ("peclet", "贝克来"), ("stanton", "斯坦顿"),
    ("stefan", "斯特藩"), ("boltzmann", "玻尔兹曼"), ("kirchhoff", "基尔霍夫"),
    ("planck", "普朗克"), ("wien", "维恩"), ("carnot", "卡诺"),
    ("rankine", "朗肯"), ("brayton", "布雷顿"), ("clausius", "克劳修斯"),
    ("kelvin", "开尔文"), ("celsius", "摄氏"), ("lmtd", "对数平均温差"),
    ("ntu", "传热单元数"), ("cop", "性能系数"),
    # Symbols a model writes when it abbreviates: Bi=毕渥, Nu=努塞尔, …
    ("bi", "毕渥"), ("nu", "努塞尔"), ("re", "雷诺"), ("pr", "普朗特"),
    ("gr", "格拉晓夫"), ("ra", "瑞利"), ("fo", "傅里叶"),
]

TERM_ALIASES: dict[str, set[str]] = {}
for _en, _zh in _TERM_PAIRS:
    TERM_ALIASES.setdefault(_en, set()).add(_zh)
    TERM_ALIASES.setdefault(_zh, set()).add(_en)


def has_alias(entity: str, allowed: set[str]) -> bool:
    """True when ``entity`` or one of its known synonyms is in ``allowed``."""
    cf = entity.casefold()
    if cf in allowed:
        return True
    return any(alias in allowed for alias in TERM_ALIASES.get(cf, ()))


_NUM_TOKEN = re.compile(r"^\d+(?:[.,]\d+)?%?$")


def looks_like_table_fragment(text: str) -> bool:
    """True for scraps of a parsed table or figure caption.

    PDF extraction flattens tables into runs of numbers, which then get lifted
    as "claims" — ``Single-turn STaR/RAFT 77.0 77.0 - - -`` is not a finding, it
    is one row of a results table. These read as authoritative and are pure
    noise, so they are dropped before they become citable.
    """
    text = (text or "").strip()
    if not text:
        return True
    tokens = text.split()
    numbers = sum(1 for t in tokens if _NUM_TOKEN.match(t))
    dashes = sum(1 for t in tokens if t and set(t) <= {"-", "–", "—"})
    if numbers >= 3:
        return True
    if numbers >= 2 and dashes >= 1:
        return True
    letters = sum(1 for ch in text if ch.isalpha() or "\u4e00" <= ch <= "\u9fff")
    return letters < 6


def _cn_int(s: str) -> str | None:
    """'二十五' -> '25', '一百' -> '100'. Only the shapes textbooks actually use."""
    if not s:
        return None
    if "百" in s:
        head, _, tail = s.partition("百")
        hundreds = 1 if head in ("", "一") else _CN_DIGITS.get(head)
        rest = _cn_int(tail) if tail else "0"
        if hundreds is None or rest is None:
            return None
        return str(int(hundreds) * 100 + int(rest))
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = 1 if head == "" else _CN_DIGITS.get(head)
        ones = _CN_DIGITS.get(tail, "0") if tail else "0"
        if tens is None:
            return None
        return str(int(tens) * 10 + int(ones))
    digits = "".join(_CN_DIGITS.get(ch, "") for ch in s)
    return digits or None


def canon_numbers(text: str) -> str:
    """Rewrite Chinese numeric forms into ASCII so both sides compare equal.

    A textbook says 毕渥数远小于零点一 while the model writes ``0.1``; the same
    value must not read as a contradiction. Only two safe shapes are converted —
    ``百分之X`` and ``X点Y``. Bare Chinese numerals are left alone because
    '十分' / '一二' would be mangled in ordinary prose.
    """
    if not text:
        return text

    def _pct(m):
        val = _cn_int(m.group(1))
        return f"{val}%" if val is not None else m.group(0)

    def _dec(m):
        whole = _cn_int(m.group(1))
        frac = "".join(_CN_DIGITS.get(ch, "") for ch in m.group(2))
        if whole is None or not frac:
            return m.group(0)
        return f"{whole}.{frac}"

    return _CN_DECIMAL.sub(_dec, _CN_PERCENT.sub(_pct, text))


def is_cjk(text: str) -> bool:
    """True when the string contains any CJK character."""
    return bool(_CJK.search(text or ""))


def entities(text: str) -> set[str]:
    """Proper-noun-ish tokens and numbers from ``text``.

    Numbers and ALL-CAPS acronyms always count. Capitalised words count only
    when they are *not* just ordinary sentence-initial capitalisation, so
    "Results show..." contributes nothing while "AlphaGo Zero" contributes
    "Zero". In a Chinese sentence any Latin token is deliberate technical
    vocabulary, so it always counts.
    """
    text = canon_numbers(text or "")
    out: set[str] = set(_NUMBER.findall(text))
    cjk = is_cjk(text)
    for match in _CAP_WORD.finditer(text):
        token = match.group(0)
        if token.isupper() or cjk:
            out.add(token)
            continue
        j = match.start() - 1
        while j >= 0 and text[j] in " \t":
            j -= 1
        if j < 0 or text[j] in _SENTENCE_END:
            continue  # sentence-initial capitalisation, not an entity
        out.add(token)
    return out


def check_support(
    claim_text: str, excerpt: str, *, crosslingual_ok: bool | None = None
) -> tuple[bool, str]:
    """Decide whether ``excerpt`` can carry ``claim_text``.

    Returns ``(ok, reason)``. ``reason`` is meant for logs, not for users.
    """
    if not claim_text or not claim_text.strip():
        return False, "claim is empty"
    if not excerpt or not excerpt.strip():
        return False, "claim has no supporting excerpt"

    if crosslingual_ok is None:
        crosslingual_ok = ALLOW_CROSSLINGUAL
    if is_cjk(claim_text) != is_cjk(excerpt):
        if crosslingual_ok:
            return True, "crosslingual allowed by config"
        return False, "claim and excerpt are in different languages"

    haystack = canon_numbers(excerpt).casefold()
    missing = sorted(
        e
        for e in entities(claim_text)
        if e.casefold() not in haystack
        and not any(a in haystack for a in TERM_ALIASES.get(e.casefold(), ()))
    )
    if missing:
        return False, "claim mentions facts absent from excerpt: " + ", ".join(missing[:5])
    return True, "ok"
