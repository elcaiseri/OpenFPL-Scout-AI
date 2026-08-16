"""Build a print-ready PDF of the technical report at static/report/.

The web report draws its figures with JavaScript, so a PDF renderer that does
not execute scripts would produce empty boxes. This script therefore:

1. runs ``scripts/report_prerender.js`` to render every chart through the real
   ``static/report/charts.js`` under Node, so the PDF can never drift from the
   page;
2. inlines those SVGs, their legends, and the generated table rows into a copy
   of ``index.html``, expanding the collapsible tables;
3. adds a print stylesheet for page size, breaks, and running footers;
4. renders the result with WeasyPrint.

Usage::

    uv run --with weasyprint python -m scripts.build_report_pdf

On macOS, WeasyPrint needs the Homebrew copies of pango and cairo::

    DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib \\
        uv run --with weasyprint python -m scripts.build_report_pdf
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = REPO_ROOT / "static" / "report"
PRERENDER = REPO_ROOT / "scripts" / "report_prerender.js"
DEFAULT_OUTPUT = REPORT_DIR / "openfpl-technical-report.pdf"

# Page furniture and the handful of screen-only affordances that do not belong
# in a paged document. Everything else is inherited from style.css.
PRINT_CSS = """
@page {
    size: A4;
    margin: 16mm 14mm 18mm;
    background: var(--bg);
    @bottom-left {
        content: "OpenFPL Scout AI — Technical Report v6.0.0";
        font-family: var(--font-body);
        font-size: 8pt;
        color: #7b6e88;
    }
    @bottom-right {
        content: counter(page) " / " counter(pages);
        font-family: var(--font-body);
        font-size: 8pt;
        color: #7b6e88;
    }
}

@page :first {
    margin: 0;
    @bottom-left { content: ""; }
    @bottom-right { content: ""; }
}

/* Keep a heading with the block that follows it. */
h2.sec, h3, h4, .sec-num, .fig-head { page-break-after: avoid; }

html, body {
    background: var(--bg) !important;
    color: var(--ink) !important;
    font-size: 9.4pt;
    line-height: 1.55;
}

* { -weasy-print-color-adjust: exact; print-color-adjust: exact; }

.wrap { max-width: none; padding: 0; }

/* Cover page ------------------------------------------------------------ */
.masthead { padding: 46mm 16mm 0; }
.masthead h1 { font-size: 44pt; margin-bottom: 8mm; }
.masthead .lede { font-size: 12pt; max-width: none; }
.meta-row { margin-top: 16mm; }
.chip { font-size: 8.5pt; }

/* The contents list takes the second page on its own. */
.toc {
    margin-top: 0;
    padding: 10mm 12mm;
    page-break-before: always;
    page-break-after: always;
    page-break-inside: avoid;
}
.toc h2 { font-size: 10pt; margin-bottom: 6mm; }
.toc ol { display: block; column-count: 2; column-gap: 14mm; }
.toc li { break-inside: avoid; }
.toc a { font-size: 10pt; padding: 2mm 0; }

/* Flow ------------------------------------------------------------------ */
/* Sections flow continuously. Forcing a break before each of the fourteen
   sections left most pages two-thirds empty; keeping headings attached to
   their first block gives the same structure without the waste. */
main section {
    padding: 5mm 0 3mm;
    border-top: 1px solid var(--line);
}
main section:first-of-type { border-top: 0; padding-top: 0; }
.sec-num { margin-bottom: 2mm; }

h2.sec { font-size: 19pt; page-break-after: avoid; }
h3 { font-size: 12pt; page-break-after: avoid; margin-top: 7mm; }
h4 { font-size: 10pt; page-break-after: avoid; }
p, li { max-width: none; }
p.intro { font-size: 10.5pt; }

/* Blocks that must not be split across a page break --------------------- */
figure, .note, .card, .diagram, .tile, .table-wrap, .legend {
    page-break-inside: avoid;
}
figure { margin: 6mm 0; }
figcaption { font-size: 8.6pt; max-width: none; }
.fig-title { font-size: 11pt; }
.fig-sub { font-size: 8.8pt; }

/* WeasyPrint's grid support is partial; use flow layouts instead. */
.tiles { display: block; }
.tiles .tile {
    display: inline-block;
    width: 47%;
    margin: 0 1% 3mm 0;
    vertical-align: top;
}
.tile .v { font-size: 20pt; }
.cols { display: block; }
.cols > div, .cols > .card {
    display: inline-block;
    width: 47.5%;
    margin: 0 1% 3mm 0;
    vertical-align: top;
}

/* Charts are fixed-viewBox SVG; scale them to the text column. */
.chart-scroll { overflow: visible; }
.chart-scroll svg, .diagram svg { width: 100%; height: auto; max-width: 100%; }
.chart-scroll.fixed-min svg { min-width: 0; }

/* The diagram labels are positioned for the page's preferred font. The PDF
   renderer substitutes a wider face, so ease the sizes back a little to keep
   every label inside the box it belongs to. */
.diagram .dg-t { font-size: 12px; }
.diagram .dg-s { font-size: 9.2px; }
.diagram .dg-label { font-size: 9.5px; }

table { min-width: 0; font-size: 8.6pt; }
th, td { padding: 3.5px 7px; }
.table-wrap { overflow: visible; }

pre { font-size: 8.2pt; padding: 8px 10px; page-break-inside: avoid; }
code { font-size: 0.9em; }

/* Screen-only affordances */
.tip { display: none !important; }
.data-table-print > summary { display: none; }

footer { margin-top: 8mm; page-break-inside: avoid; }
footer p { font-size: 8.6pt; }

a { color: var(--ink-2); text-decoration: none; }
"""


def _prerender() -> dict:
    """Render every figure and table through the page's own chart code."""
    node = shutil.which("node")
    if node is None:
        raise SystemExit("node is required to pre-render the report figures")
    result = subprocess.run(
        [node, str(PRERENDER), str(REPORT_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"Figure pre-render failed:\n{result.stderr.strip()}")
    return json.loads(result.stdout)


def _inline_figures(html: str, rendered: dict) -> str:
    """Replace the empty chart mounts with rendered SVG and legends."""
    for figure_id, svg in rendered["figures"].items():
        pattern = re.compile(
            r'<div class="(chart-scroll[^"]*)" id="' + re.escape(figure_id) + r'"></div>'
        )
        if not pattern.search(html):
            raise SystemExit(f"Could not find the mount for {figure_id}")
        legend = rendered["legends"].get(figure_id, "")
        html = pattern.sub(
            lambda match: legend + f'<div class="{match.group(1)}">{svg}</div>',
            html,
            count=1,
        )
    return html


def _fill_tables(html: str, rendered: dict) -> str:
    """Populate the tbody elements the page would otherwise fill at runtime."""
    for table_id, rows in rendered["tables"].items():
        pattern = re.compile(r'<tbody id="' + re.escape(table_id) + r'"></tbody>')
        if not pattern.search(html):
            raise SystemExit(f"Could not find the tbody for {table_id}")
        html = pattern.sub(f'<tbody id="{table_id}">{rows}</tbody>', html, count=1)
    return html


def _expand_details(html: str) -> str:
    """A paged document has no disclosure widgets; show the tables outright."""
    html = html.replace('<details class="data-table">', '<details class="data-table-print" open>')
    return html.replace("</details>", "</details>")


def _strip_scripts(html: str) -> str:
    return re.sub(r'\s*<script src="[^"]+"></script>', "", html)


def _inline_assets(html: str, css: str) -> str:
    """Embed the stylesheet so the PDF build needs no file resolution."""
    return html.replace(
        '<link rel="stylesheet" href="style.css">',
        f"<style>{css}</style>\n<style>{PRINT_CSS}</style>",
    )


def build(output_path: Path) -> Path:
    try:
        from weasyprint import HTML
    except ImportError as error:  # pragma: no cover - environment guidance
        raise SystemExit(
            "WeasyPrint is required. Run:\n"
            "  DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib \\\n"
            "    uv run --with weasyprint python -m scripts.build_report_pdf"
        ) from error

    rendered = _prerender()
    html = (REPORT_DIR / "index.html").read_text(encoding="utf-8")
    css = (REPORT_DIR / "style.css").read_text(encoding="utf-8")

    html = _inline_figures(html, rendered)
    html = _fill_tables(html, rendered)
    html = _expand_details(html)
    html = _strip_scripts(html)
    html = _inline_assets(html, css)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # base_url lets the renderer resolve the logo and any other relative asset.
    HTML(string=html, base_url=str(REPORT_DIR)).write_pdf(str(output_path))
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Destination PDF (default: {DEFAULT_OUTPUT.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--keep-html",
        type=Path,
        default=None,
        help="Also write the assembled print HTML here, for debugging",
    )
    args = parser.parse_args(argv)

    if args.keep_html is not None:
        rendered = _prerender()
        html = (REPORT_DIR / "index.html").read_text(encoding="utf-8")
        css = (REPORT_DIR / "style.css").read_text(encoding="utf-8")
        html = _inline_assets(
            _strip_scripts(_expand_details(_fill_tables(_inline_figures(html, rendered), rendered))),
            css,
        )
        args.keep_html.write_text(html, encoding="utf-8")
        print(f"Wrote {args.keep_html}")

    path = build(args.output)
    size_kb = path.stat().st_size / 1024
    print(f"Wrote {path.relative_to(REPO_ROOT)} ({size_kb:,.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
