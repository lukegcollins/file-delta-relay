"""Render a branch's architecture-report Markdown into print-styled HTML.

Why this exists: the design document was previously maintained as two hand-kept
copies, a Markdown one and an HTML one. They drifted -- an audit found the HTML
still describing mechanisms the code had stopped using, because a change had
been applied to one copy and not the other. A generator makes that class of bug
structurally impossible: the Markdown is the source, the HTML is output, and a
stale HTML means someone forgot to run one command rather than forgot to edit a
second file.

The Markdown subset supported is exactly what the design document uses -- ATX
headings, paragraphs, bold/italic/inline code, fenced code and ```mermaid
blocks, pipe tables, unordered and ordered lists, blockquotes, horizontal rules
and inline links. It is deliberately hand-rolled rather than pulling in a
Markdown library: this is a repository whose premise is a minimal dependency
footprint, and the subset is small enough that a parser for it is shorter than
the argument for adding a dependency.

Run:
    python3 tools/render_writeup.py                    # -> docs/lightweight-portable/architecture_report_lightweight_full.html
    python3 tools/render_writeup.py --inline-mermaid -i docs/main/architecture_report_main_full.md -o /tmp/print.html

--inline-mermaid embeds mermaid.min.js in the page instead of linking the CDN,
which is what makes headless-Chrome PDF export deterministic and offline. The
committed HTML keeps the CDN link so the file stays a few tens of KB rather
than a few megabytes.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@10.9.8/dist/mermaid.min.js"
MERMAID_LOCAL = os.path.join(ROOT, "sync-demo", "figures", "node_modules",
                             "mermaid", "dist", "mermaid.min.js")

STYLE = """
  @page { size: Letter; margin: 0.7in 0.75in; }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 10.6pt; line-height: 1.47; color: #14171a;
  }
  h1 { font-size: 17pt; margin: 0 0 4pt 0; font-weight: 700; letter-spacing: -0.012em; }
  h2 {
    font-size: 12.2pt; margin: 16pt 0 6pt 0; padding-bottom: 3pt;
    border-bottom: 1px solid #d7dade; font-weight: 700; letter-spacing: -0.005em;
    break-after: avoid; page-break-after: avoid;
  }
  h2:first-of-type { margin-top: 4pt; }
  h3 {
    font-size: 10.9pt; margin: 11pt 0 4pt 0; font-weight: 700; color: #24282c;
    break-after: avoid; page-break-after: avoid;
  }
  p { margin: 0 0 7pt 0; }
  .subtitle { font-size: 9.2pt; color: #55595e; font-style: italic; margin: 0 0 12pt 0; }
  strong { font-weight: 700; }
  code {
    font-family: "SF Mono", Consolas, Menlo, monospace; font-size: 9.4pt;
    background: #f2f3f5; padding: 0.5pt 3pt; border-radius: 3px;
  }
  pre.pipeline {
    font-family: "SF Mono", Consolas, Menlo, monospace; font-size: 9pt;
    background: #f6f7f8; border: 1px solid #e3e5e8; border-radius: 4px;
    padding: 6pt 9pt; margin: 5pt 0 9pt 0; text-align: center; color: #2a2d31;
    overflow-x: auto;
  }
  ul, ol { margin: 0 0 8pt 0; padding-left: 17pt; }
  li { margin: 0 0 3pt 0; }
  blockquote {
    margin: 0 0 8pt 0; padding: 5pt 10pt; border-left: 3px solid #c8ccd1;
    background: #f7f8f9; color: #3a3e42;
  }
  blockquote p:last-child { margin-bottom: 0; }
  table {
    border-collapse: collapse; width: 100%; margin: 4pt 0 10pt 0; font-size: 9.5pt;
    break-inside: avoid; page-break-inside: avoid;
  }
  th, td { border: 1px solid #dfe2e5; padding: 4pt 7pt; text-align: left; vertical-align: top; }
  th { background: #f4f5f7; font-weight: 700; }
  .mermaid { text-align: center; margin: 6pt 0 4pt 0;
             break-inside: avoid; page-break-inside: avoid; }
  .mermaid svg { max-height: 4.6in !important; width: auto !important; max-width: 100% !important; }
  .caption {
    font-size: 8.7pt; font-style: italic; color: #55595e; text-align: center;
    margin: 0 0 11pt 0;
  }
  .caption code { font-size: 8.5pt; }
  hr { border: none; border-top: 1px solid #d7dade; margin: 13pt 0 8pt 0; }
  .footer-note { font-size: 8.9pt; font-style: italic; color: #55595e; margin: 0; }
  .footer-note code { font-size: 8.7pt; }
  a { color: #14171a; text-decoration: underline; text-decoration-color: #b9bec4; }
"""

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
{mermaid_tag}
<style>{style}</style>
</head>
<body>

{body}
<script>
  mermaid.initialize({{ startOnLoad: true, theme: "neutral", securityLevel: "loose",
    themeVariables: {{ fontSize: "13px" }} }});
</script>
</body>
</html>
"""


def inline(text: str) -> str:
    """Convert inline Markdown (code, bold, italic, links) to HTML.

    Inline code is extracted first and restored last so that emphasis and link
    syntax inside a code span is rendered literally rather than interpreted --
    `*ptr` in a code span must stay `*ptr`.
    """
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append(html.escape(m.group(1)))
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = html.escape(text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{spans[int(m.group(1))]}</code>", text)


def _table(rows: list[str]) -> str:
    """Render a pipe-table block, whose second row is the alignment separator."""
    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    head, body = cells(rows[0]), [cells(r) for r in rows[2:]]
    out = ["<table>", "<thead><tr>"]
    out += [f"<th>{inline(c)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for r in body:
        out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def render(md: str) -> tuple[str, str]:
    """Return (title, body HTML) for one Markdown document."""
    lines = md.split("\n")
    out: list[str] = []
    title = "Design Document"
    i, n = 0, len(lines)
    first_para = True

    while i < n:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        if line.startswith("```"):
            lang = line[3:].strip()
            i += 1
            block = []
            while i < n and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            content = "\n".join(block)
            if lang == "mermaid":
                # Mermaid reads the element's own text, so it must NOT be
                # HTML-escaped into entities -- but it must also not be able to
                # close the tag early. Escaping only '<' satisfies both.
                out.append(f'<pre class="mermaid">{content.replace("<", "&lt;")}</pre>')
            else:
                out.append(f'<pre class="pipeline">{html.escape(content)}</pre>')
            continue

        if line.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            block = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.append(_table(block))
            continue

        if re.match(r"^---+\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            level, text = len(m.group(1)), m.group(2)
            if level == 1:
                title = re.sub(r"[*`]", "", text)
            out.append(f"<h{level}>{inline(text)}</h{level}>")
            i += 1
            continue

        if line.startswith(">"):
            block = []
            while i < n and lines[i].startswith(">"):
                block.append(lines[i].lstrip(">").strip())
                i += 1
            out.append(f"<blockquote><p>{inline(' '.join(block))}</p></blockquote>")
            continue

        if re.match(r"^\s*[-*]\s+", line) or re.match(r"^\s*\d+\.\s+", line):
            ordered = bool(re.match(r"^\s*\d+\.\s+", line))
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while i < n and lines[i].strip():
                cur = lines[i]
                if re.match(r"^\s*[-*]\s+", cur) or re.match(r"^\s*\d+\.\s+", cur):
                    items.append(re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", cur))
                else:
                    items[-1] += " " + cur.strip()      # continuation line
                i += 1
            out.append(f"<{tag}>" + "".join(f"<li>{inline(t)}</li>" for t in items) + f"</{tag}>")
            continue

        para = []
        while i < n and lines[i].strip() and not lines[i].startswith(("#", "|", "```", ">")) \
                and not re.match(r"^---+\s*$", lines[i]) \
                and not re.match(r"^\s*(?:[-*]|\d+\.)\s+", lines[i]):
            para.append(lines[i].strip())
            i += 1
        text = " ".join(para)
        wholly_italic = text.startswith("*") and text.endswith("*") and not text.startswith("**")
        if wholly_italic and first_para:
            out.append(f'<p class="subtitle">{inline(text[1:-1])}</p>')
        elif wholly_italic and text.startswith("*Figure"):
            out.append(f'<p class="caption">{inline(text[1:-1])}</p>')
        elif wholly_italic:
            out.append(f'<p class="footer-note">{inline(text[1:-1])}</p>')
        else:
            out.append(f"<p>{inline(text)}</p>")
        first_para = False

    return title, "\n".join(out)


def main() -> int:
    """Render the Markdown design document to HTML."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--input", default=os.path.join(
        ROOT, "docs", "lightweight-portable", "architecture_report_lightweight_full.md"))
    ap.add_argument("-o", "--output", default=os.path.join(
        ROOT, "docs", "lightweight-portable", "architecture_report_lightweight_full.html"))
    ap.add_argument("--inline-mermaid", action="store_true",
                    help="embed mermaid.min.js instead of linking the CDN "
                         "(needed for offline/headless PDF export)")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        title, body = render(f.read())

    if args.inline_mermaid:
        if not os.path.exists(MERMAID_LOCAL):
            print(f"mermaid.min.js not found at {MERMAID_LOCAL}; "
                  f"run `npm install` in sync-demo/figures", file=sys.stderr)
            return 1
        with open(MERMAID_LOCAL, encoding="utf-8") as f:
            mermaid_tag = f"<script>{f.read()}</script>"
    else:
        mermaid_tag = (f'<script src="{MERMAID_CDN}" crossorigin="anonymous">'
                       f"</script>")

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(PAGE.format(title=html.escape(title), style=STYLE,
                            mermaid_tag=mermaid_tag, body=body))
    print(f"wrote {args.output} ({os.path.getsize(args.output):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
