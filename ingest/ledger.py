"""Per-stage time and cost log (`ledger.jsonl`) and budget guard."""
from __future__ import annotations

import json
import time

from ingest.util import work_dir

# Estimated unit prices ($/1M tokens), from usageMetadata; thinking tokens billed at the output price.
PRICES = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-transcribe": (2.0, 12.0),
}
DEFAULT_BUDGET_USD = 5.0


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    def __init__(self, sha: str, budget_usd: float = DEFAULT_BUDGET_USD):
        self.path = work_dir(sha) / "ledger.jsonl"
        self.budget = budget_usd

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]

    def spent(self) -> float:
        return round(sum(e.get("cost_usd", 0.0) for e in self.entries()), 5)

    def guard(self, next_est: float) -> None:
        s = self.spent()
        if s + next_est > self.budget:
            raise BudgetExceeded(f"budget stop: spent ${s:.3f} + next ~${next_est:.3f} > ${self.budget:.2f}")

    def log(self, stage: str, status: str, elapsed_s: float, cost_usd: float = 0.0, **extra) -> dict:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stage": stage, "status": status,
               "elapsed_s": round(elapsed_s, 2), "cost_usd": round(cost_usd, 5), **extra}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        return rec


def cost_generate(model: str, usage: dict) -> float:
    pin, pout = PRICES[model]
    tin = usage.get("prompt_token_count", 0) or 0
    tout = (usage.get("candidates_token_count", 0) or 0) + (usage.get("thoughts_token_count", 0) or 0)
    return tin / 1e6 * pin + tout / 1e6 * pout


def cost_interaction(model: str, usage: dict) -> float:
    pin, pout = PRICES[model]
    tin = usage.get("total_input_tokens", 0) or 0
    tout = (usage.get("total_output_tokens", 0) or 0) + (usage.get("total_thought_tokens", 0) or 0)
    return tin / 1e6 * pin + tout / 1e6 * pout
