#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
from core import DOMAINS, STACKS, search_domain, search_stack


def main() -> int:
    parser = argparse.ArgumentParser(description="Search local Workshop Design Studio guidance")
    parser.add_argument("query")
    parser.add_argument("--domain", choices=sorted(DOMAINS))
    parser.add_argument("--stack", choices=sorted(STACKS))
    parser.add_argument("--max-results", "-n", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.stack:
        result = search_stack(args.query, args.stack, args.max_results)
    else:
        result = search_domain(args.query, args.domain or "ux", args.max_results)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    print(f"# {result.get('domain') or result.get('stack')} results for: {args.query}")
    if result.get("count") == 0:
        print("No local match. Broaden the query; do not present fallback advice as retrieved guidance.")
        if result.get("suggestions"):
            print("Suggestions:", ", ".join(result["suggestions"]))
        return 0

    for i, item in enumerate(result.get("results", []), 1):
        print(f"\n## {i}")
        for key, value in item.items():
            if key != "_score" and value:
                print(f"- **{key}:** {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
