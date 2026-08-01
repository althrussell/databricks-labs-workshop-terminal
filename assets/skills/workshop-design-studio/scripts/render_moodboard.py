#!/usr/bin/env python3
# Render a dependency-free HTML moodboard from design-system.json.
from __future__ import annotations

import argparse
from html import escape
import json
from pathlib import Path


def safe(value, default=""):
    return escape(str(value or default))


def render(data: dict, project: str) -> str:
    palette = data.get("palette", {})
    typography = data.get("typography", {})
    layout = data.get("layout", {})
    imagery = data.get("imagery", {})
    voice = data.get("voice", {})
    motion = data.get("motion", {})
    style = data.get("style", {})
    dials = data.get("dials", {})

    colors = [
        ("Background", palette.get("background", "#F7F7F5")),
        ("Surface", palette.get("surface", "#FFFFFF")),
        ("Foreground", palette.get("foreground", "#171717")),
        ("Primary", palette.get("primary", "#315EFB")),
        ("Accent", palette.get("accent", "#FF6A3D")),
        ("Muted", palette.get("muted", "#6B7280")),
    ]
    swatches = "".join(
        f'''<article class="swatch">
          <div class="swatch-color" style="background:{safe(color)}"></div>
          <strong>{safe(label)}</strong><code>{safe(color)}</code>
        </article>'''
        for label, color in colors
    )

    dial_markup = "".join(
        f'''<div class="dial"><span>{safe(name.replace("_", " ").title())}</span>
        <div class="track"><i style="width:{max(0, min(10, int(value))) * 10}%"></i></div>
        <b>{safe(value)}/10</b></div>'''
        for name, value in dials.items()
    )

    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe(project)} — Design Moodboard</title>
<style>
  :root {{
    --bg: {safe(palette.get("background"), "#F7F7F5")};
    --surface: {safe(palette.get("surface"), "#FFFFFF")};
    --fg: {safe(palette.get("foreground"), "#171717")};
    --primary: {safe(palette.get("primary"), "#315EFB")};
    --accent: {safe(palette.get("accent"), "#FF6A3D")};
    --muted: {safe(palette.get("muted"), "#6B7280")};
    --display: {safe(typography.get("display_stack"), "system-ui")};
    --body: {safe(typography.get("body_stack"), "system-ui")};
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--fg); font-family: var(--body); line-height: 1.5; }}
  main {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 56px 0 96px; }}
  header {{ display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(260px, .7fr); align-items: end; gap: 40px; padding-bottom: 48px; border-bottom: 1px solid color-mix(in srgb, var(--fg) 18%, transparent); }}
  .eyebrow {{ color: var(--primary); font-weight: 750; letter-spacing: .08em; text-transform: uppercase; font-size: 12px; }}
  h1 {{ font-family: var(--display); font-size: clamp(48px, 8vw, 104px); line-height: .92; letter-spacing: -.055em; margin: 12px 0 20px; max-width: 10ch; }}
  .lede {{ font-size: clamp(18px, 2vw, 25px); max-width: 48ch; color: var(--muted); }}
  .signature {{ background: var(--fg); color: var(--bg); padding: 24px; border-radius: 20px; transform: rotate(1deg); box-shadow: 0 24px 60px color-mix(in srgb, var(--fg) 18%, transparent); }}
  .signature strong {{ color: var(--accent); display: block; margin-bottom: 8px; }}
  section {{ padding: 48px 0; border-bottom: 1px solid color-mix(in srgb, var(--fg) 14%, transparent); }}
  h2 {{ font-family: var(--display); font-size: clamp(28px, 4vw, 48px); line-height: 1; letter-spacing: -.03em; margin: 0 0 24px; }}
  .grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px; }}
  .panel {{ background: var(--surface); border: 1px solid color-mix(in srgb, var(--fg) 14%, transparent); border-radius: 16px; padding: 24px; }}
  .panel p {{ color: var(--muted); }}
  .swatches {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; }}
  .swatch {{ min-width: 0; }}
  .swatch-color {{ aspect-ratio: 1; border-radius: 14px; border: 1px solid color-mix(in srgb, var(--fg) 15%, transparent); margin-bottom: 10px; }}
  .swatch strong, .swatch code {{ display:block; overflow-wrap:anywhere; font-size: 12px; }}
  .swatch code {{ color: var(--muted); }}
  .type-sample.display {{ font-family: var(--display); font-size: clamp(42px, 6vw, 76px); line-height: .95; letter-spacing: -.045em; margin: 0; }}
  .type-sample.body {{ font-family: var(--body); font-size: 20px; max-width: 62ch; color: var(--muted); }}
  .dial {{ display: grid; grid-template-columns: 140px 1fr 52px; gap: 14px; align-items: center; margin: 12px 0; }}
  .track {{ height: 8px; border-radius: 99px; background: color-mix(in srgb, var(--fg) 12%, transparent); overflow:hidden; }}
  .track i {{ display:block; height:100%; background: linear-gradient(90deg, var(--primary), var(--accent)); border-radius:inherit; }}
  .button {{ display:inline-flex; align-items:center; gap:10px; background:var(--primary); color:white; padding:13px 18px; border-radius:10px; font-weight:750; }}
  @media (max-width: 820px) {{
    header, .grid {{ grid-template-columns: 1fr; }}
    .swatches {{ grid-template-columns: repeat(3, 1fr); }}
  }}
  @media (max-width: 480px) {{
    main {{ width:min(100% - 24px, 1180px); padding-top:32px; }}
    .swatches {{ grid-template-columns: repeat(2, 1fr); }}
    .dial {{ grid-template-columns: 1fr 42px; }}
    .track {{ grid-column: 1 / -1; grid-row: 2; }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .signature {{ transform:none; }}
  }}
</style>
</head>
<body>
<main>
  <header>
    <div>
      <div class="eyebrow">{safe(data.get("product"), "Product")} · {safe(data.get("mode"), "product-led")}</div>
      <h1>{safe(project)}</h1>
      <p class="lede">{safe(data.get("goal"), "A distinctive product experience")}</p>
      <span class="button">Primary action <span aria-hidden="true">→</span></span>
    </div>
    <aside class="signature">
      <strong>Signature moment</strong>
      {safe(data.get("signature_moment"))}
    </aside>
  </header>

  <section>
    <h2>Creative direction</h2>
    <div class="grid">
      <article class="panel"><strong>Style</strong><h3>{safe(style.get("style"))}</h3><p>{safe(style.get("description"))}</p></article>
      <article class="panel"><strong>Composition</strong><h3>{safe(layout.get("name"))}</h3><p>{safe(layout.get("structure"))}</p></article>
      <article class="panel"><strong>Imagery</strong><h3>{safe(imagery.get("name"))}</h3><p>{safe(imagery.get("direction"))}</p></article>
    </div>
  </section>

  <section>
    <h2>Palette</h2>
    <div class="swatches">{swatches}</div>
  </section>

  <section>
    <h2>Typography</h2>
    <p class="type-sample display">Design with a point of view.</p>
    <p class="type-sample body">{safe(voice.get("body"), "Clear, credible supporting copy gives the visual system substance.")}</p>
  </section>

  <section>
    <h2>Expression controls</h2>
    {dial_markup}
  </section>

  <section>
    <h2>Interaction</h2>
    <div class="grid">
      <article class="panel"><strong>Motion</strong><h3>{safe(motion.get("name"))}</h3><p>{safe(motion.get("implementation"))}</p></article>
      <article class="panel"><strong>Voice</strong><h3>{safe(voice.get("name"))}</h3><p>{safe(voice.get("headline"))}</p></article>
      <article class="panel"><strong>Avoid</strong><h3>Protect the idea</h3><p>{safe("; ".join(data.get("avoid", [])))}</p></article>
    </div>
  </section>
</main>
</body>
</html>'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", type=Path, default=Path(".design-studio/design-system.json"))
    parser.add_argument("--output", type=Path, default=Path(".design-studio/moodboard.html"))
    parser.add_argument("--project-name")
    args = parser.parse_args()

    data = json.loads(args.system.read_text(encoding="utf-8"))
    project = args.project_name or data.get("product") or "Workshop Project"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(data, project), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
