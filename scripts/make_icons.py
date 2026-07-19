"""Generate the Tesla Invoices icon set from one SVG template.

Outputs (all from the same artwork):
  - favicon.svg          dark by default, light via prefers-color-scheme
  - icon-*.png           transparent-corner app icons (dark / light)
  - maskable-*.png       full-bleed variants for the PWA maskable purpose
  - apple-touch-icon.png 180px full-bleed dark
"""


import sys
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")

DARK = {
    "tile_top": "#3a3d44",
    "tile_bottom": "#141619",
    "tile_stroke": "rgba(255,255,255,0.10)",
    "doc_fill": "rgba(255,255,255,0.13)",
    "doc_stroke": "rgba(255,255,255,0.32)",
    "fold_fill": "rgba(255,255,255,0.30)",
    "line": "rgba(255,255,255,0.40)",
}
LIGHT = {
    "tile_top": "#ffffff",
    "tile_bottom": "#d7dbe1",
    "tile_stroke": "rgba(0,0,0,0.12)",
    "doc_fill": "rgba(255,255,255,0.85)",
    "doc_stroke": "rgba(0,0,0,0.22)",
    "fold_fill": "rgba(0,0,0,0.10)",
    "line": "rgba(0,0,0,0.30)",
}
RED = "#e31b23"
RED_GLOW = "#ff2d33"


def art(p: dict, maskable: bool = False) -> str:
    """The icon artwork; ``maskable`` renders a full-bleed square background
    with the motif scaled into the safe zone (central 80%)."""
    if maskable:
        tile = '<rect x="0" y="0" width="512" height="512" fill="url(#tile)"/>'
        group_open = '<g transform="translate(51.2,51.2) scale(0.8)">'
    else:
        tile = (
            '<rect x="8" y="8" width="496" height="496" rx="112" fill="url(#tile)" '
            f'stroke="{p["tile_stroke"]}" stroke-width="4"/>'
        )
        group_open = "<g>"
    return f"""
  <defs>
    <radialGradient id="tile" cx="0.5" cy="0.18" r="1.1">
      <stop offset="0" stop-color="{p["tile_top"]}"/>
      <stop offset="1" stop-color="{p["tile_bottom"]}"/>
    </radialGradient>
    <radialGradient id="glow" cx="0.5" cy="0.5" r="0.5">
      <stop offset="0" stop-color="{RED_GLOW}" stop-opacity="0.38"/>
      <stop offset="1" stop-color="{RED_GLOW}" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="bolt" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{RED_GLOW}"/>
      <stop offset="1" stop-color="{RED}"/>
    </linearGradient>
  </defs>
  {tile}
  {group_open}
    <!-- glass invoice sheet with a folded corner and text lines -->
    <path d="M150 138 a14 14 0 0 1 14 -14 h150 l58 58 v226 a14 14 0 0 1 -14 14 h-194 a14 14 0 0 1 -14 -14 z"
          fill="{p["doc_fill"]}" stroke="{p["doc_stroke"]}" stroke-width="5"/>
    <path d="M314 124 l58 58 h-44 a14 14 0 0 1 -14 -14 z" fill="{p["fold_fill"]}"/>
    <g fill="{p["line"]}">
      <rect x="176" y="160" width="96" height="13" rx="6.5"/>
      <rect x="176" y="190" width="120" height="13" rx="6.5"/>
      <rect x="176" y="288" width="60" height="13" rx="6.5"/>
      <rect x="286" y="288" width="50" height="13" rx="6.5"/>
      <rect x="176" y="318" width="72" height="13" rx="6.5"/>
      <rect x="286" y="318" width="50" height="13" rx="6.5"/>
      <rect x="176" y="348" width="52" height="13" rx="6.5"/>
      <rect x="286" y="348" width="50" height="13" rx="6.5"/>
    </g>
    <!-- soft red glow behind the emblem -->
    <ellipse cx="256" cy="266" rx="150" ry="150" fill="url(#glow)"/>
    <!-- the T-bolt: Tesla-style swept crossbar, lightning-bolt stem -->
    <g fill="url(#bolt)">
      <path d="M104 178 C 170 138 342 138 408 178 L 374 232 C 322 200 190 200 138 232 Z"/>
      <path d="M276 196 L 202 340 L 248 340 L 190 448 L 326 296 L 276 296 L 338 196 Z"/>
    </g>
  </g>
"""


def svg(p: dict, maskable: bool = False) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
        + art(p, maskable)
        + "</svg>"
    )


def render(svg_text: str, out: Path, size: int) -> None:
    import cairosvg

    cairosvg.svg2png(
        bytestring=svg_text.encode(),
        write_to=str(out),
        output_width=size,
        output_height=size,
    )
    print("wrote", out)


def favicon_svg() -> str:
    """One SVG that follows the OS theme: dark artwork by default, light
    palette applied via prefers-color-scheme. Styling uses CSS variables so
    only the palette switches, not the geometry."""
    dark_vars = "".join(f"--{k}: {v};" for k, v in DARK.items())
    light_vars = "".join(f"--{k}: {v};" for k, v in LIGHT.items())
    body = art({k: f"var(--{k})" for k in DARK})
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <style>
    :root {{ {dark_vars} }}
    @media (prefers-color-scheme: light) {{ :root {{ {light_vars} }} }}
  </style>
  {body}
</svg>
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "favicon.svg").write_text(favicon_svg())
    print("wrote", OUT / "favicon.svg")

    dark, light = svg(DARK), svg(LIGHT)
    render(dark, OUT / "icon-512.png", 512)
    render(dark, OUT / "icon-192.png", 192)
    render(light, OUT / "icon-light-512.png", 512)
    render(svg(DARK, maskable=True), OUT / "maskable-512.png", 512)
    render(svg(DARK, maskable=True), OUT / "maskable-192.png", 192)
    render(svg(DARK, maskable=True), OUT / "apple-touch-icon.png", 180)


if __name__ == "__main__":
    main()
