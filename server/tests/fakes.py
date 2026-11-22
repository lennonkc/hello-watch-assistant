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
