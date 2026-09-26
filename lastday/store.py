"""Stored plans and their append-only journals, kept on local disk (gitignored).

A journal line is written after every step attempt, so an interrupted run leaves an
accurate record. Whether a step still needs doing is always decided from live GitHub
state; the journal is the receipt, not the source of truth.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path


class PlanStore:
    def __init__(self, root: Path):
        self.plans = root / "plans"
        self.journals = root / "journals"

    @classmethod
    def from_env(cls) -> "PlanStore":
        return cls(Path(os.environ.get("LASTDAY_STATE_DIR", ".lastday")))

    def save(self, plan: dict) -> None:
        self.plans.mkdir(parents=True, exist_ok=True)
        path = self.plans / f"{plan['hash']}.json"
        if not path.exists():
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            temporary.replace(path)

    def load(self, plan_hash: str) -> dict | None:
        path = self.plans / f"{plan_hash}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def record(self, plan_hash: str, event: str, **details: object) -> None:
        self.journals.mkdir(parents=True, exist_ok=True)
        entry = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **details}
        with (self.journals / f"{plan_hash}.jsonl").open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(entry) + "\n")

    def entries(self, plan_hash: str) -> list[dict]:
        path = self.journals / f"{plan_hash}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def step_status(self, plan_hash: str) -> dict[int, dict]:
        """The latest journal entry for each step index."""
        latest: dict[int, dict] = {}
        for entry in self.entries(plan_hash):
            if entry["event"] == "step":
                latest[entry["index"]] = entry
        return latest
