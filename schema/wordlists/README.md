# Shared wordlists `schema/wordlists/` (spec §7.4)

The knowledge-pack builder, the on-device online service (the `watch` module) and the evaluation tooling **must use the same version**
of these wordlists and the same normalization algorithm.
Each knowledge pack records the version used at build time in `pack_meta.wordlists_version` (= the contents of `VERSION` in this directory).

## Files

| File | Format | Purpose |
|---|---|---|
| `VERSION` | one line: the `wordlists_version` (currently `1`) | version number |
| `en_common.txt` | one lowercase English word per line (`[a-z]+`), sorted; lines starting with `#` are comments | §7.1 decision on standalone real-person given/family names; proper-noun allowlist for the online service |
| `t2s.tsv` | `traditional<TAB>simplified`, single-character mapping; `#` comments | normalization step 4 |
| `normalize_vectors.tsv` | `input<TAB>folded<TAB>case_kept`; `#` comments | conformance test vectors for every implementation |
| `normalize.py` | Python standard library | reference implementation + self-test |
| `build_wordlists.py` | requires wordfreq | lets maintainers regenerate `en_common.txt` / `t2s.tsv` |
| `LICENSES/` | | full texts of third-party licenses |

All files are UTF-8 (no BOM), LF line endings, with a trailing newline on the last line.

## Normalization algorithm

`normalize(text, case_sensitive)`, with the steps in a fixed order:

1. **NFKC**: folds full-width/half-width forms, ligatures (ﬁ), Roman numerals (Ⅻ), superscripts, circled digits and
   compatibility ideographs (U+F900 etc.) into their standard forms.
2. **NFD, then delete every code point with general category `Mn`**: strips accents (é→e, Å→A, Ż→Z, İ→I).
3. **NFC**.
4. **Per-code-point Traditional→Simplified**: look up `t2s.tsv`; code points not in the table are kept as is. The table is
   resolved to a fixed point, so the result is idempotent.
5. **Unicode lowercase when `case_sensitive = 0`** (Python `str.lower` / Rust `str::to_lowercase`),
   **not** casefold (`ß` stays `ß`, it does not become `ss`).

The result is idempotent in both modes: `normalize(normalize(x)) == normalize(x)`.

Edge cases to be aware of (all covered by test vectors):

- **Letters without a decomposition are kept as is**: `Ł ł Ø ø Æ æ ß Đ ı` are not "base letter + combining mark", so step 2
  leaves them alone; `Łukasz` → `łukasz`, not `lukasz`. The builder produces variants with this algorithm and does not invent an `L` version.
- **Possessives are not handled by normalization**: `Scales's` and `Sintel’s` (U+2019) are output unchanged. Possessives (`'s` / `’s` / trailing `'`)
  are handled by the matcher (spec §7.1); the full-width apostrophe `＇` becomes `'` under NFKC, but `’` is unaffected by NFKC, so matchers must recognize both.
- **Chinese punctuation**: full-width `？，：` etc. become half-width under NFKC; `「」、。` are not compatibility characters and are kept as is.
- **Side effects on non-Latin scripts**: step 2 removes every Mn, including Japanese dakuten (`ガ`→`カ`), Greek tonos, and vowel signs in Hindi and similar scripts.
  This has no effect on en/zh, but must be re-evaluated before extending to other languages.
- Traditional→Simplified is a lossy, single-character mapping that takes OpenCC's first candidate (`乾`→`干`, `髮`/`發`→`发`). Both sides use the same algorithm, so matching is unaffected.
- `normalize_vectors.tsv` deliberately contains decomposed sequences (`e` + U+0301) and compatibility ideographs (U+F900); do not let your editor apply Unicode normalization when editing it.

## Using `en_common.txt`, and its trade-offs

- **Lookup**: run `normalize(case_sensitive=0)` on the candidate, strip any possessive suffix, then look up the whole word. The list contains only `[a-z]+`,
  with no possessive, contracted or hyphenated forms.
- **§7.1**: a standalone real-person given or family name is written to `blocklist` only if it is ≥5 characters long **and** not in this list.
- **Proper-noun allowlist**: a capitalized word mid-sentence (not sentence-initial) must be a visible character name/alias/location/object, or be in this list.
- **Contents**: words from the SCOWL size ≤50 pool with wordfreq Zipf ≥3.0 (at least once per million words), about 22,000 words;
  includes common nouns/adjectives/verbs and forms of address (mom, dad, sir, lady, king, dragon, shaman, ...),
  plus 42 "always capitalized" common words (i, weekdays, months, common country/language adjectives, christmas, mr/mrs/ms/dr, tv).
- **Personal-name filter (conservative: better to miss a common word than to admit a personal name)**: common given names from the 1990 US Census are
  removed if SCOWL also lists their capitalized proper-noun form and the lowercase usage is not in SCOWL's core tiers (lowest lowercase size ≥40, or size 35 with census rank ≤200).
  v1 removes 61 (john, tom, peter, mike, harry, lee, maria, ...; also homographs of common nouns such as robin, ruby, holly,
  heather, carol). Homographs in the core tier (size ≤20) are kept (will, mark, bill, rose, grace, jack, frank).
  Family names are not filtered: baker, miller, smith, hunter are common words anyway.
- Consequence: actors' names that coincide with common words (e.g. Grace, Hunter) are not listed separately in `blocklist`, and pass the allowlist rule.
  This is a trade-off already accepted in spec §7.1: the full name is still in `blocklist`, and the "character–actor relation word" rule acts as a backstop.

## Sources and licenses

| Data | Source | License |
|---|---|---|
| `en_common.txt` vocabulary | [SCOWL](http://wordlist.aspell.net/) 2020.12.07 (Kevin Atkinson et al.) | SCOWL Copyright (MIT-style permissive license; the copyright notice must be retained), full text in `LICENSES/SCOWL-Copyright.txt` |
| `en_common.txt` frequency filter | [wordfreq](https://github.com/rspeer/wordfreq) 3.1.1 (Robyn Speer) | code Apache-2.0, data CC BY-SA 4.0. Used only as an inclusion threshold; this repo does not distribute frequency values. Acknowledged here |
| `en_common.txt` personal-name filter | [1990 US Census name lists](https://www2.census.gov/topics/genealogy/1990surnames/) | US government work, public domain |
| `t2s.tsv` | [OpenCC](https://github.com/BYVoid/OpenCC) `data/dictionary/TSCharacters.txt` (commit in the file header) | Apache-2.0, full text in `LICENSES/OpenCC-Apache-2.0.txt`; modifications described in the file header |
| All other files | this repo | MIT (repository `LICENSE`) |

The sha256 of every input file is recorded in the header of the corresponding output.

## Self-test

```bash
python3 -I schema/wordlists/normalize.py --selftest      # all vectors + file-format checks
python3 -I schema/wordlists/normalize.py "Ｓｃａｌｅｓ與小鱗"   # → scales与小鳞
```

Implementations in other languages (the on-device Rust service, evaluation scripts) should read `normalize_vectors.tsv` and run the same vectors.

## Bumping the version

1. Change the wordlists or the normalization algorithm (any change that alters `normalize` output or lookup results counts).
   To regenerate, download the upstream data to a temporary directory outside the repo and run
   `python3 -I schema/wordlists/build_wordlists.py --scowl … --census … --opencc … --opencc-commit …` (arguments are described in the script docstring).
2. Increment `VERSION` (integer); when the algorithm changes, update `normalize.py`, this README and the test vectors accordingly.
3. Run `--selftest`.
4. The builder writes the new version to `pack_meta.wordlists_version`; packs built with an older version must be rebuilt with the new one,
   and the online service should check on load that a pack's `wordlists_version` matches the wordlists it ships with.
