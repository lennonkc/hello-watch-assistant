"""Test doubles: a scripted ADK model (no network) and helpers to drive /api/ask end to end."""
from __future__ import annotations

import asyncio
import json

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

USAGE = types.GenerateContentResponseUsageMetadata(prompt_token_count=1000, candidates_token_count=20,
                                                   thoughts_token_count=5, total_token_count=1025)


def tools_off(req) -> bool:
    """True if function calls are forbidden in this request (an LlmRequest or a recorded request dict)."""
    tc = (req["config"] if isinstance(req, dict) else req.config).tool_config
    return bool(tc and tc.function_calling_config and tc.function_calling_config.mode == types.FunctionCallingConfigMode.NONE)


class FakeLlm(BaseLlm):
    """Each model call takes the next step of `script` (a dict, or a callable `req -> dict`):
    `{"text": str | [chunks], "gap": s, "sleep": s}` streams text (partials, then the aggregated response);
    `{"call": (name, args), "pre": "text"}` returns a function call, optionally after some text.
    Every request is recorded in `requests` as {"contents", "config", "stream"}."""
    model: str = "fake"
    script: list = Field(default_factory=list)
    requests: list = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream: bool = False):
        self.requests.append({"contents": list(llm_request.contents), "config": llm_request.config.model_copy(deep=True),
                              "stream": stream})
        step = self.script.pop(0) if self.script else {"text": "Nothing more to add."}
        if callable(step):
            step = step(llm_request)
        if step.get("sleep"):
            await asyncio.sleep(step["sleep"])
        if "call" in step:
            name, args = step["call"]
            pre = [types.Part(text=step["pre"])] if step.get("pre") else []
            if stream and pre:
                yield LlmResponse(content=types.Content(role="model", parts=pre), partial=True)
            yield LlmResponse(content=types.Content(role="model", parts=pre + [
                types.Part(function_call=types.FunctionCall(name=name, args=args))]), usage_metadata=USAGE)
            return
        chunks = step["text"] if isinstance(step["text"], list) else [step["text"]]
        if stream:
            for c in chunks:
                yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=c)]), partial=True)
                if step.get("gap"):
                    await asyncio.sleep(step["gap"])
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="".join(chunks))]),
                          partial=False, usage_metadata=USAGE)


def sse_events(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = block.split("\n")
        out.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return out


def request_text(req: dict) -> str:
    """All text the model received in one request (system instruction excluded)."""
    return "\n".join(p.text for c in req["contents"] for p in (c.parts or []) if p.text)


def request_images(req: dict) -> list[bytes]:
    return [p.inline_data.data for c in req["contents"] for p in (c.parts or []) if p.inline_data]


# ── an independent leak oracle for the sweeps ───────────────────────

MARGIN_REVEAL = 1.0          # gate_margin_reviewed_reveal_s of the test pack


def oracle_leaks(pack: dict, text: str, P: float, prose: bool = True) -> list[str]:
    """What must not be in `text` at P, checked with plain substrings straight from the pack's rows — no guard, no
    textnorm, no word boundaries — so a matcher bug cannot hide a leak from the sweep. `prose`: also the identity
    co-occurrence rule, built from the pack rows on its own terms (any word of a label, a pronoun continuing the
    sentence before) rather than from the guard's reference patterns; off for structured lists such as the character
    list, where both characters legitimately appear as separate entries."""
    import re
    low, out = text.lower(), []
    hidden = lambda known: known is None or P + 1e-9 < known + MARGIN_REVEAL      # noqa: E731
    for c in pack["characters"]:
        if not c["name_public"] and hidden(c["name_known_at_s"]):
            for n in (c["canonical_name"], c["name_zh"]):
                if n and n.lower() != (c["display_label_pre_name"] or "").lower() and n in text:
                    out.append(n)
    for a in pack.get("character_aliases", []):
        if not a["generic"] and hidden(a["alias_known_at_s"]) and a["alias"] in text:
            out.append(a["alias"])
    out += [b["term"] for b in pack["blocklist"] if b["term"].lower() in low]
    for k in pack["canaries"]:
        if P + 1e-9 < k["active_until_s"]:
            hit = (re.search(k["term"], text, 0 if k["case_sensitive"] else re.IGNORECASE) if k["match"] == "regex"
                   else (k["term"] in text if k["case_sensitive"] else k["term"].lower() in low))
            if hit:
                out.append(k["term"])
    chars = {c["character_id"]: c for c in pack["characters"]}
    for lk in pack["identity_links"] if prose else []:
        if lk["review_state"] != "rejected" and P + 1e-9 < lk["reveal_at_s"] + MARGIN_REVEAL:
            a, b = chars[lk["a_character_id"]], chars[lk["b_character_id"]]
            a_words, b_words = _ways(pack, a), _ways(pack, b)
            for s in _passages(text):
                if any(w in s.lower() for w in a_words) and any(w in s.lower() for w in b_words):
                    out.append(f"{a['display_label_pre_name']} + {b['display_label_pre_name']}")
    return out


def _ways(pack: dict, c: dict) -> set[str]:
    """Lower-case substrings that refer to a character: every word of its labels except articles (so "the grey
    wolf" gives "grey" and "wolf"), its Chinese label, its names and aliases."""
    import re
    out = {w for lab in (c["display_label_pre_name"], c["canonical_name"]) if lab
           for w in re.findall(r"[a-z]+", lab.lower()) if w not in ("the", "a", "an") and len(w) > 2}
    out |= {t.lower() for t in (c["display_label_pre_name_zh"], c["name_zh"], c["canonical_name"]) if t}
    out |= {x["alias"].lower() for x in pack.get("character_aliases", []) if x["character_id"] == c["character_id"]}
    return out


def _passages(text: str) -> list[str]:
    """Sentences (newlines end one too); a sentence that opens with a pronoun, or has it / he / she in its first few
    words, continues the one before."""
    import re
    out = []
    for s in (x.strip() for x in re.split(r"(?<=[.!?。！？])|\n", text)):
        if not s:
            continue
        first = " ".join(s.lower().split()[:5])
        if out and (s[:1] in "它他她这那" or re.search(r"\b(it|he|she|they|this|that)\b", first)):
            out[-1] += " " + s
        else:
            out.append(s)
    return out
