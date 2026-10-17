#!/usr/bin/env python3
"""Regenerate en_common.txt and t2s.tsv (for maintainers; not needed at runtime).

Requires wordfreq==3.1.1 (used only as a frequency-filter signal). Download the inputs beforehand to a temporary directory outside the repo:
  --scowl   the final/ directory of the unpacked SCOWL 2020.12.07 archive
            https://downloads.sourceforge.net/project/wordlist/SCOWL/2020.12.07/scowl-2020.12.07.tar.gz
  --census  directory containing dist.male.first / dist.female.first (1990 US Census name lists, public domain)
            https://www2.census.gov/topics/genealogy/1990surnames/
  --opencc  OpenCC data/dictionary/TSCharacters.txt (Apache-2.0)
  --opencc-commit  the OpenCC commit the file above comes from (written to the file header)

  python3 -I schema/wordlists/build_wordlists.py --scowl DIR --census DIR \
      --opencc FILE --opencc-commit SHA
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from normalize import strip_marks  # noqa: E402

SCOWL_SPELLINGS = ("english", "american", "british", "canadian")
SCOWL_LEVELS = (10, 20, 35, 40, 50)  # both the vocabulary pool and the proper-noun forms use SCOWL size <= 50
ZIPF_MIN = 3.0  # at least once per million words
COMMON_GIVEN_NAME_RANK = 200

# SCOWL's lowercase lists omit common words that are "always capitalized"; they routinely appear capitalized mid-sentence, so add them to avoid allowlist false positives.
SUPPLEMENT = """
i
monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october november december
english american british chinese french german japanese spanish italian russian korean
indian european african asian christian christmas
mr mrs ms dr tv
""".split()


def sha256(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def load_scowl(final_dir: str):
    word_level: dict[str, int] = {}
    name_level: dict[str, int] = {}
    for level in SCOWL_LEVELS:
        for sp in SCOWL_SPELLINGS:
            for cat in ("words", "upper", "proper-names"):
                path = os.path.join(final_dir, f"{sp}-{cat}.{level}")
                if not os.path.exists(path):
                    continue
                for line in open(path, encoding="latin-1"):
                    w = line.rstrip("\n")
                    if cat == "words":
                        w = strip_marks(w).lower()
                        if re.fullmatch(r"[a-z]+", w):  # drop possessive/contracted/hyphenated forms
                            word_level[w] = min(word_level.get(w, 999), level)
                    else:
                        name_level[w] = min(name_level.get(w, 999), level)
    return word_level, name_level


def load_given_names(census_dir: str) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for fn in ("dist.male.first", "dist.female.first"):
        for line in open(os.path.join(census_dir, fn), encoding="ascii"):
            name, _, _, rank = line.split()
            ranks[name.lower()] = min(ranks.get(name.lower(), 10**9), int(rank))
    return ranks


def is_name_dominant(w: str, level: int, name_level: dict[str, int], given: dict[str, int]) -> bool:
    """Ordinary personal name: a common given name, SCOWL also lists its capitalized proper-noun form, and the lowercase usage is not in the core tier (≤20)."""
    if w not in given or name_level.get(w.capitalize(), 999) > 50:
        return False
    return level >= 40 or (level >= 35 and given[w] <= COMMON_GIVEN_NAME_RANK)


def build_en(args) -> None:
    from importlib.metadata import version

    from wordfreq import zipf_frequency

    wf_version = version("wordfreq")

    word_level, name_level = load_scowl(args.scowl)
    given = load_given_names(args.census)
    selected = {w for w in word_level if zipf_frequency(w, "en") >= ZIPF_MIN}
    dropped = sorted(w for w in selected if is_name_dominant(w, word_level[w], name_level, given))
    words = sorted((selected - set(dropped)) | set(SUPPLEMENT))

    census_sha = ", ".join(
        f"{fn} sha256={sha256(os.path.join(args.census, fn))}"
        for fn in ("dist.male.first", "dist.female.first")
    )
    header = f"""\
# en_common.txt — 常用英语词表(C1 §7.4),每行一个小写词,按字典序排列
# 用途:§7.1 真人单独姓/名是否为常用词;在线服务专名白名单
# 词数:{len(words)}
#
# 来源与许可:
#   词汇池 = SCOWL 2020.12.07(Kevin Atkinson 等),size <= 50,english/american/british/canadian 拼写
#     http://wordlist.aspell.net/  许可:SCOWL Copyright(MIT 类宽松许可,全文见 LICENSES/SCOWL-Copyright.txt)
#   频率筛选信号 = wordfreq {wf_version} 的 zipf_frequency(w, "en") >= {ZIPF_MIN}(Robyn Speer,https://github.com/rspeer/wordfreq,
#     代码 Apache-2.0,数据 CC BY-SA 4.0)。本文件只含 SCOWL 中的词,不含任何频率数值;致谢 wordfreq。
#   人名过滤 = 1990 美国人口普查名字表 dist.male.first / dist.female.first(美国政府作品,公有领域)
#     https://www2.census.gov/topics/genealogy/1990surnames/  {census_sha}
#
# 生成方法(schema/wordlists/build_wordlists.py):
#   1. SCOWL words 表 size <= 50,按 normalize.py 步骤 1–3 去重音后小写,只保留 [a-z]+(所有格/缩写/连字符形式不收)
#   2. 保留 zipf >= {ZIPF_MIN} 的词
#   3. 剔除普通人名:是普查名字 且 SCOWL 有大写专名形式(size <= 50) 且
#      (小写最低 size >= 40,或 size = 35 且普查名次 <= {COMMON_GIVEN_NAME_RANK});本次剔除 {len(dropped)} 个,如 {", ".join(dropped[:12])}
#      核心层(size <= 20)的同形词保留(will, mark, bill, rose, grace, jack, frank ...)
#   4. 补充"总是大写"的常用词 {len(SUPPLEMENT)} 个:i、星期、月份、常见国家/语言形容词、christmas、mr/mrs/ms/dr、tv
"""
    with open(os.path.join(HERE, "en_common.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(header)
        f.write("\n".join(words) + "\n")
    print(f"en_common.txt: {len(words)} words, dropped {len(dropped)} names", file=sys.stderr)


def build_t2s(args) -> None:
    raw: dict[str, str] = {}
    multi = 0
    for line in open(args.opencc, encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        key, values = line.rstrip("\n").split("\t", 1)
        if len(key) != 1:
            multi += 1
            continue
        raw[key] = values.split(" ")[0]

    table: dict[str, str] = {}
    skipped = 0
    for key, val in raw.items():
        # resolve chained mappings to a fixed point so that t2s(t2s(x)) == t2s(x)
        seen = {key}
        while val in raw and raw[val] != val and val not in seen:
            seen.add(val)
            val = raw[val]
        if key == val:
            continue
        if strip_marks(key) != key or strip_marks(val) != val or len(val) != 1:
            skipped += 1  # compatibility characters that cannot survive NFKC; mapping them is pointless
            continue
        table[key] = val

    header = f"""\
# t2s.tsv — 繁→简单字映射(C1 §7.4),格式:繁<TAB>简
# 条目:{len(table)}
#
# 来源:OpenCC data/dictionary/TSCharacters.txt
#   https://github.com/BYVoid/OpenCC/blob/{args.opencc_commit}/data/dictionary/TSCharacters.txt
#   commit {args.opencc_commit},sha256={sha256(args.opencc)}
# 许可:Apache-2.0(Copyright BYVoid 及 OpenCC 贡献者;全文见 LICENSES/OpenCC-Apache-2.0.txt)
# 修改说明(Apache-2.0 §4b):由脚本 schema/wordlists/build_wordlists.py 转换——
#   每个繁体字只取第一个候选;去掉恒等映射;链式映射解析到不动点(幂等);
#   跳过 NFKC/去重音后不稳定的码位({skipped} 个)与多字键({multi} 个)
"""
    with open(os.path.join(HERE, "t2s.tsv"), "w", encoding="utf-8", newline="\n") as f:
        f.write(header)
        for key in sorted(table):
            f.write(f"{key}\t{table[key]}\n")
    print(f"t2s.tsv: {len(table)} entries, skipped {skipped}, multi {multi}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scowl", required=True)
    p.add_argument("--census", required=True)
    p.add_argument("--opencc", required=True)
    p.add_argument("--opencc-commit", required=True)
    args = p.parse_args()
    build_t2s(args)
    build_en(args)


if __name__ == "__main__":
    main()
