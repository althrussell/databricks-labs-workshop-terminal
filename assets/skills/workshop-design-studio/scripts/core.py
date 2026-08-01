#!/usr/bin/env python3
"""Dependency-free local retrieval for Workshop Design Studio."""
from __future__ import annotations

import csv
import math
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DOMAINS = {
    "product": ("products.csv", ["product", "keywords", "design_goal", "recommended_pattern", "avoid"]),
    "style": ("styles.csv", ["style", "keywords", "description", "best_for", "avoid"]),
    "palette": ("palettes.csv", ["name", "keywords", "notes", "mode"]),
    "typography": ("typography.csv", ["name", "keywords", "notes"]),
    "layout": ("layouts.csv", ["name", "keywords", "structure", "best_for", "notes"]),
    "motion": ("motion.csv", ["name", "keywords", "tier", "implementation", "guardrail"]),
    "ux": ("ux-guidelines.csv", ["category", "issue", "keywords", "description", "do", "dont"]),
    "chart": ("charts.csv", ["question", "keywords", "primary", "use_when", "avoid"]),
    "icon": ("icons.csv", ["category", "examples", "guidance"]),
    "imagery": ("imagery.csv", ["name", "keywords", "direction", "implementation", "avoid"]),
    "voice": ("voice.csv", ["name", "keywords", "headline", "body", "microcopy", "avoid"]),
}

STACKS = {p.stem: p for p in (DATA / "stacks").glob("*.csv")}

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "the", "to", "with", "we", "our", "this",
}
SYNONYMS = {
    "colour": "color", "colours": "color", "a11y": "accessibility",
    "e-commerce": "ecommerce", "dark-mode": "dark", "darkmode": "dark",
    "web-site": "website", "admin panel": "operations", "dashboard": "analytics",
    "chatbot": "ai assistant", "copilot": "ai assistant", "devtool": "developer tool",
}


def normalise(text: str) -> str:
    value = str(text).lower()
    for source, target in SYNONYMS.items():
        value = value.replace(source, target)
    value = re.sub(r"[^a-z0-9+#.-]+", " ", value)
    return " ".join(value.split())


def tokens(text: str) -> list[str]:
    return [t for t in normalise(text).split() if len(t) > 1 and t not in STOPWORDS]


class BM25Index:
    def __init__(self, documents: list[str], k1: float = 1.45, b: float = 0.72):
        self.docs = [tokens(d) for d in documents]
        self.k1, self.b = k1, b
        self.lengths = [len(d) for d in self.docs]
        self.avg = sum(self.lengths) / max(1, len(self.lengths))
        self.freqs = [Counter(d) for d in self.docs]
        df: Counter[str] = Counter()
        for doc in self.docs:
            df.update(set(doc))
        n = max(1, len(self.docs))
        self.idf = {term: math.log(1 + (n - count + 0.5) / (count + 0.5)) for term, count in df.items()}

    def rank(self, query: str) -> list[tuple[int, float]]:
        q = tokens(query)
        ranked = []
        for i, freqs in enumerate(self.freqs):
            score = 0.0
            for term in q:
                tf = freqs.get(term, 0)
                if not tf or term not in self.idf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * self.lengths[i] / max(self.avg, 0.01))
                score += self.idf[term] * (tf * (self.k1 + 1)) / denom
            ranked.append((i, score))
        return sorted(ranked, key=lambda pair: pair[1], reverse=True)

    def suggestions(self, query: str, limit: int = 6) -> list[str]:
        q = tokens(query)
        candidates = []
        for term in self.idf:
            if any(term.startswith(x[:3]) or x.startswith(term[:3]) for x in q if len(x) >= 3):
                candidates.append((self.idf[term], term))
        return [term for _, term in sorted(candidates, reverse=True)[:limit]]


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def search_domain(query: str, domain: str, limit: int = 3) -> dict:
    if domain not in DOMAINS:
        return {"error": f"unknown domain {domain}", "available": sorted(DOMAINS)}
    filename, columns = DOMAINS[domain]
    path = DATA / filename
    rows = load_rows(path)
    documents = [" ".join(row.get(c, "") for c in columns) for row in rows]
    index = BM25Index(documents)
    matches = []
    for idx, score in index.rank(query):
        if score <= 0:
            continue
        item = dict(rows[idx])
        item["_score"] = round(score, 4)
        matches.append(item)
        if len(matches) >= limit:
            break
    return {
        "query": query,
        "domain": domain,
        "source": filename,
        "count": len(matches),
        "results": matches,
        "suggestions": [] if matches else index.suggestions(query),
    }


def search_stack(query: str, stack: str, limit: int = 5) -> dict:
    path = STACKS.get(stack)
    if not path:
        return {"error": f"unknown stack {stack}", "available": sorted(STACKS)}
    rows = load_rows(path)
    columns = ["category", "guideline", "description", "do", "dont", "severity"]
    docs = [" ".join(row.get(c, "") for c in columns) for row in rows]
    index = BM25Index(docs)
    matches = []
    for idx, score in index.rank(query):
        if score <= 0:
            continue
        item = dict(rows[idx])
        item["_score"] = round(score, 4)
        matches.append(item)
        if len(matches) >= limit:
            break
    if not matches:
        matches = rows[:limit]
    return {
        "query": query,
        "stack": stack,
        "source": str(path.relative_to(DATA)),
        "results": matches,
        "count": len(matches),
    }
