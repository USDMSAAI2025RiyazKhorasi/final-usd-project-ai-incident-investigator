"""Shared runbook search: BM25 over knowledge/runbooks/*.md, chunked by heading.

Pure Python (no rank_bm25 dependency). Any agent may call search_runbooks;
config flag runbook_search=false removes the tool (ablation A4).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

RUNBOOK_DIR = Path(__file__).resolve().parents[1] / "knowledge" / "runbooks"
_TOKEN = re.compile(r"[a-z0-9]+")
STOP = set("the a an and or of to in on for is are be it this that with as by at from if then when".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOP]


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.avgdl = sum(map(len, docs)) / max(1, len(docs))
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in docs]

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for d, tf in zip(self.docs, self.tf):
            s = 0.0
            for q in query:
                if q in tf:
                    f = tf[q]
                    s += self.idf[q] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / self.avgdl))
            out.append(s)
        return out


@lru_cache(maxsize=4)
def load_index(directory: str = str(RUNBOOK_DIR)) -> tuple[list[dict], BM25]:
    chunks = []
    for path in sorted(Path(directory).glob("*.md")):
        if path.name.startswith("_"):
            continue
        title, section, buf = path.stem, "", []

        def flush():
            if buf:
                text = "\n".join(buf).strip()
                if text:
                    chunks.append({"id": f"{path.stem}#{len(chunks)}", "source": path.name,
                                   "title": title, "section": section, "text": text})

        for line in path.read_text().splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
            elif line.startswith("## "):
                flush()
                buf, section = [], line[3:].strip()
            else:
                buf.append(line)
        flush()
    return chunks, BM25([tokenize(f"{c['title']} {c['section']} {c['text']}") for c in chunks])


def search(query: str, k: int = 3, min_score: float = 1.0, directory: str = str(RUNBOOK_DIR)) -> list[dict]:
    chunks, index = load_index(directory)
    scored = sorted(zip(index.scores(tokenize(query)), chunks), key=lambda x: -x[0])
    return [{**c, "score": round(s, 3)} for s, c in scored[:k] if s >= min_score]
