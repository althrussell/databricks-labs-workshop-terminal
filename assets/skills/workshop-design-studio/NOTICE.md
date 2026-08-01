# Notice and attribution

`workshop-design-studio` is a fork-only Workshop Terminal skill. It is
maintained in this repository and is not vendored from
`databricks/databricks-agent-skills`, so it sits outside the reviewed upstream
skills digest and is preserved across a skills refresh via `FORK_ONLY` in
`scripts/refresh_vendored_skills.py`.

## Attribution

The skill's local-retrieval architecture, persistent master/page design-system
model, design dials, explicit no-match disclosure, and multi-domain design
workflow were informed by **UI UX Pro Max** by Next Level Builder
(`https://github.com/nextlevelbuilder/ui-ux-pro-max-skill`, reviewed at commit
`14ddef5c05e52d7c253b8f0129de7bcd1045ae5b`), which is MIT licensed.

Concepts studied from that project:

- local BM25-style retrieval over structured design data;
- multi-domain design-system generation;
- persistent master/page override structure;
- explicit design dials;
- stack detection and stack-specific guidance;
- zero-result disclosure rather than fabricated matches;
- machine-readable output;
- accessibility and pre-delivery quality checks.

The scripts and curated design datasets in this directory are independently
written for the Workshop Terminal. No claim is made that this skill is an
official derivative, distribution, or endorsed edition of that project. The
upstream licence is reproduced below because the project materially influenced
the design and implementation approach.

## Upstream licence

```text
MIT License

Copyright (c) 2024 Next Level Builder

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Trademarks

Databricks, AppKit, Claude, Codex, React, and other referenced product names
and trademarks belong to their respective owners. This skill does not bundle or
redistribute those products.
