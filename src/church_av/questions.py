"""A log of questions the operator asked, so the booth can offer the common ones."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


class QuestionLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()

    def append(self, question: str, answer: str) -> None:
        question = " ".join(question.split())
        answer = answer.strip()
        if not question or not answer:
            return
        record = {
            "question": question,
            "answer": answer,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def frequent(self, limit: int = 3) -> list[str]:
        """The questions asked most often, in the wording first used."""
        counts: dict[str, tuple[int, str]] = {}
        with self._lock:
            if not self.path.is_file():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            question = " ".join(str(record.get("question", "")).split())
            if not question:
                continue
            key = question.casefold()
            count, label = counts.get(key, (0, question))
            counts[key] = (count + 1, label)
        ranked = sorted(counts.values(), key=lambda item: (-item[0], item[1]))
        return [label for _count, label in ranked[:limit]]
