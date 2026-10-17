#!/usr/bin/env python3
"""Reference implementation of shared-wordlist normalization (spec §7.2 / §7.4). Standard library only.

    normalize(text, case_sensitive=False)

Algorithm (fixed order; every implementation must match it step by step):
  1. NFKC
  2. NFD, then delete every code point with general category == Mn (strip accents)
  3. NFC
  4. Per-code-point Traditional->Simplified (t2s.tsv, single-character mapping; code points not in the table are kept)
  5. Unicode lowercase when case_sensitive=False (str.lower, not casefold)

Self-test:   python3 -I schema/wordlists/normalize.py --selftest
Single text: python3 -I schema/wordlists/normalize.py [--case-sensitive] TEXT...
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))

_t2s_cache: dict[str, dict[str, str]] = {}


def load_t2s(path: str | None = None) -> dict[str, str]:
    path = path or os.path.join(HERE, "t2s.tsv")
    if path not in _t2s_cache:
        table: dict[str, str] = {}
        with open(path, encoding="utf-8", newline="\n") as f:
            for lineno, line in enumerate(f, 1):
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) != 2 or len(parts[0]) != 1 or len(parts[1]) != 1:
                    raise ValueError(f"{path}:{lineno}: 需要 '繁<TAB>简' 单字映射: {line!r}")
                table[parts[0]] = parts[1]
        _t2s_cache[path] = table
    return _t2s_cache[path]


def strip_marks(text: str) -> str:
    """Steps 1–3: NFKC → NFD and drop Mn → NFC."""
    text = unicodedata.normalize("NFKC", text)
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return unicodedata.normalize("NFC", text)


def normalize(text: str, case_sensitive: bool = False, t2s: dict[str, str] | None = None) -> str:
    table = load_t2s() if t2s is None else t2s
    text = strip_marks(text)
    text = "".join(table.get(c, c) for c in text)
    if not case_sensitive:
        text = text.lower()
    return text


# ---------------------------------------------------------------- selftest


def _read_lines(path: str) -> list[str]:
    raw = open(path, "rb").read()
    problems = []
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("含 BOM")
    if b"\r" in raw:
        problems.append("含 CR(须为 LF)")
    if raw and not raw.endswith(b"\n"):
        problems.append("末行缺换行")
    text = raw.decode("utf-8")  # non-UTF-8 raises right here
    if problems:
        raise AssertionError(f"{os.path.basename(path)}: " + ", ".join(problems))
    return text.split("\n")[:-1]


def selftest() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # VERSION
    version_lines = _read_lines(os.path.join(HERE, "VERSION"))
    check(len(version_lines) == 1 and version_lines[0].strip() == version_lines[0] != "",
          f"VERSION 须为单行非空: {version_lines!r}")

    # t2s.tsv: single characters, no identity mappings, normalization-stable, idempotent (no value is also a key)
    _read_lines(os.path.join(HERE, "t2s.tsv"))
    table = load_t2s()
    check(len(table) > 1000, f"t2s.tsv 条目过少: {len(table)}")
    for k, v in table.items():
        if k == v:
            failures.append(f"t2s 恒等映射: {k}")
        if strip_marks(k) != k or strip_marks(v) != v:
            failures.append(f"t2s 非归一化稳定字符: {k}->{v}")
        if v in table:
            failures.append(f"t2s 非幂等: {k}->{v}->{table[v]}")

    # en_common.txt: lowercase [a-z]+, deduplicated, sorted, unchanged by normalization
    words = [w for w in _read_lines(os.path.join(HERE, "en_common.txt")) if not w.startswith("#")]
    check(all(re.fullmatch(r"[a-z]+", w) for w in words), "en_common.txt 存在非 [a-z]+ 行")
    check(words == sorted(set(words)), "en_common.txt 须去重且按字典序排列")
    check(10000 <= len(words) <= 30000, f"en_common.txt 词数异常: {len(words)}")
    common = set(words)
    for w in ("dragon", "shaman", "mom", "dad", "sir", "scales", "wounded", "i"):
        check(w in common, f"en_common.txt 缺常用词: {w}")
    for w in ("john", "mary", "sintel"):
        check(w not in common, f"en_common.txt 不应含人名/片内专名: {w}")

    # test vectors
    vectors = 0
    for lineno, line in enumerate(_read_lines(os.path.join(HERE, "normalize_vectors.tsv")), 1):
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            failures.append(f"normalize_vectors.tsv:{lineno}: 须为 3 列: {line!r}")
            continue
        src, folded, kept = parts
        vectors += 1
        got_f = normalize(src)
        got_k = normalize(src, case_sensitive=True)
        if got_f != folded:
            failures.append(f"vector:{lineno} folded {src!r}: got {got_f!r}, want {folded!r}")
        if got_k != kept:
            failures.append(f"vector:{lineno} case_kept {src!r}: got {got_k!r}, want {kept!r}")
        for out, cs in ((got_f, False), (got_k, True)):
            if normalize(out, case_sensitive=cs) != out:
                failures.append(f"vector:{lineno} 非幂等 (case_sensitive={cs}): {out!r}")
    check(vectors >= 30, f"测试向量不足 30 条: {vectors}")

    for msg in failures:
        print("FAIL", msg)
    print(f"selftest: {vectors} vectors, {len(table)} t2s, {len(words)} words, "
          f"{len(failures)} failure(s)")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--selftest"]:
        return selftest()
    case_sensitive = False
    if argv[:1] == ["--case-sensitive"]:
        case_sensitive, argv = True, argv[1:]
    if not argv:
        print(__doc__.strip())
        return 2
    for arg in argv:
        print(normalize(arg, case_sensitive=case_sensitive))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
