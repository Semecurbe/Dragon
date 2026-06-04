"""
Converts all documents in a directory into Quarto pages (.qmd)
and computes hierarchical chunking via docling.
"""

import base64
import json
import re
import sys
from pathlib import Path
from dataclasses import dataclass, field, asdict

from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.chunking import HierarchicalChunker
from docling_core.types.doc import ImageRefMode


SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".html", ".md", ".adoc", ".xlsx"}

_DRAGON_NAVBAR_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 165 48">
<g transform="translate(24,26) scale(0.22)">
<path d="M-100,55 L-102,12 L-90,-25 L-98,-58 L-78,-28 L-60,-105 L-32,-22 L-5,-32 L25,-26 L55,-14 L82,0 L100,16 L92,26 L62,20 L94,42 L70,48 L35,56 L-5,64 L-55,62 Z" fill="#C0392B"/>
<path d="M-78,-28 L-60,-105 L-45,-40" fill="#A93226" opacity="0.7"/>
<path d="M-90,-25 L-98,-58 L-78,-28" fill="#A93226" opacity="0.5"/>
<path d="M62,20 L92,26 L100,16 L115,8 L125,14 L118,22 L94,42 Z" fill="#E67E22" opacity="0.85"/>
<ellipse cx="12" cy="-8" rx="9" ry="8" fill="rgba(255,255,255,0.92)"/>
<circle cx="16" cy="-8" r="4.5" fill="#1C2833"/>
<circle cx="18" cy="-10" r="1.5" fill="rgba(255,255,255,0.6)"/>
</g>
<text x="58" y="31" font-family="'Segoe UI','Helvetica Neue',Arial,sans-serif" font-size="22" font-weight="600" letter-spacing="2">
<tspan fill="#C0392B">DRAG</tspan><tspan fill="#ffffff">ON</tspan>
</text>
</svg>"""

# Heading names that identify a table-of-contents section (not content sections)
_TOC_HEADING_RE = re.compile(
    r'^(?:'
    r'table\s+of\s+contents?'
    r'|table\s+des\s+mati[eè]res?'
    r'|sommaire'
    r'|contents?'
    r')\s*$',
    re.IGNORECASE,
)

# Lines that look like TOC entries even without a labeled heading:
#   "I - Introduction .............. 3"  or  "Chapter 1  5"
_TOC_ENTRY_RE = re.compile(
    r'^.{2,80}(?:\.{3,}|─{3,}|-{3,})\s*\d{1,4}\s*$'
)


def strip_toc_and_summary(markdown: str) -> str:
    """Remove the table-of-contents section from docling-generated markdown.

    Only removes sections whose heading is "Sommaire", "Table des matières",
    "Table of Contents" or "Contents".  All actual content headings
    (Introduction, Chapitre 1, …) are preserved untouched.
    """
    lines = markdown.splitlines(keepends=True)
    out: list[str] = []
    skip_level: int | None = None

    for line in lines:
        m = re.match(r'^(#{1,6})\s+(.*)', line.rstrip('\n\r'))
        if skip_level is not None:
            # Stop skipping when we reach a heading at the same or higher level
            if m and len(m.group(1)) <= skip_level:
                skip_level = None
                out.append(line)
            # else: inside the TOC section → drop the line
        else:
            if m and _TOC_HEADING_RE.match(m.group(2).strip()):
                # Start of a TOC section — skip it and its content
                skip_level = len(m.group(1))
            else:
                out.append(line)

    return ''.join(out)


def is_toc_chunk(chunk_data: dict) -> bool:
    """Return True if this chunk belongs to a TOC section (not a content section)."""
    headings = chunk_data.get('metadata', {}).get('headings', [])
    return any(_TOC_HEADING_RE.match(h.strip()) for h in headings)


@dataclass
class ChunkData:
    index: int
    text: str
    metadata: dict = field(default_factory=dict)


def chunk_to_dict(chunk, index: int) -> dict:
    return ChunkData(
        index=index,
        text=chunk.text,
        metadata=chunk.meta.export_json_dict() if hasattr(chunk.meta, "export_json_dict") else {},
    ).__dict__


def extract_images(markdown: str, images_dir: Path, stem: str) -> str:
    """
    Finds every base64-encoded image in the markdown, saves it as a file
    in images_dir, and replaces the inline data URI with a relative path.
    Returns the updated markdown.
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    counter = 0

    def save_and_replace(match: re.Match) -> str:
        nonlocal counter
        media_type = match.group(1)          # e.g. "image/png"
        b64_data   = match.group(2)

        ext = media_type.split("/")[-1]
        ext = "jpg" if ext == "jpeg" else ext

        counter += 1
        filename = f"image_{counter:03d}.{ext}"
        (images_dir / filename).write_bytes(base64.b64decode(b64_data))

        return f"![Image]({stem}_files/{filename})"

    pattern = r'!\[Image\]\(data:(image/[^;]+);base64,([^)]*)\)'
    return re.sub(pattern, save_and_replace, markdown)


def write_quarto_assets(quarto_dir: Path) -> None:
    """Write static assets (logo SVG) required by the Quarto site."""
    (quarto_dir / "dragon-navbar.svg").write_text(_DRAGON_NAVBAR_SVG, encoding="utf-8")


def build_quarto_page(title: str, markdown_content: str) -> str:
    frontmatter = f"""---
title: "{title}"
---

"""
    return frontmatter + markdown_content


def build_quarto_yml(site_title: str, app_url: str = "http://localhost:7860") -> str:
    return f"""project:
  type: website
  output-dir: _site

website:
  title: "{site_title}"
  favicon: "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🐉</text></svg>"
  navbar:
    background: dark
    foreground: light
    logo: "dragon-navbar.svg"
    logo-alt: "Dragon"
    title: false
    left:
      - href: index.qmd
        text: "📚 Documentation"
      - text: "💬 Chat"
        href: "{app_url}/chat"
      - text: "📝 Sources"
        href: "{app_url}/qmd"
      - text: "📤 Ingest"
        href: "{app_url}/ingest"
    right:
      - text: "⚙️ Settings"
        href: "{app_url}/settings"
      - text: "🐉 About"
        href: "{app_url}/about"

format:
  html:
    theme: darkly
    toc: true
    toc-location: right
    toc-depth: 3
    toc-title: "On this page"
    page-layout: article
    include-in-header:
      text: |
        <style>
          html, body {{
            font-family: 'Segoe UI', system-ui, sans-serif;
            background-color: #0d1117 !important;
            color: #e6edf3 !important;
          }}
          #quarto-content, #quarto-document-content, .quarto-container,
          main.content, .page-columns, .column-body,
          #quarto-margin-sidebar, #quarto-sidebar, .sidebar {{
            background-color: #0d1117 !important;
          }}
          .column-body {{
            background-color: #161b22 !important;
            border-radius: 12px;
            border: 1px solid #30363d !important;
            padding: 1.6rem !important;
          }}
          .navbar {{
            background-color: #0d1117 !important;
            box-shadow: 0 2px 8px rgba(0,0,0,.5);
            border-bottom: 1px solid #30363d;
          }}
          .navbar-brand img {{ height: 38px !important; width: auto; display: block; }}
          .navbar .nav-link {{ color: rgba(255,255,255,.7) !important; }}
          .navbar .nav-link:hover,
          .navbar .nav-link.active {{ color: #fff !important; }}
          .navbar-brand {{ color: #fff !important; font-weight: 700; }}
          h1, h2, h3, h4, h5, h6 {{ color: #e6edf3 !important; }}
          h2, h3 {{ border-bottom: 1px solid #30363d; padding-bottom: .3rem; }}
          p, li, td, th, blockquote {{ color: #c9d1d9 !important; }}
          a {{ color: #58a6ff !important; }}
          a:hover {{ color: #79c0ff !important; }}
          .sidebar nav, #TOC {{ background-color: #0d1117 !important; }}
          #TOC a, .sidebar a {{ color: #8b949e !important; }}
          #TOC a:hover, #TOC li.active > a {{ color: #58a6ff !important; }}
          pre, pre code, .sourceCode {{
            background-color: #1e1e2e !important;
            color: #cdd6f4 !important;
            border: 1px solid #30363d;
            border-radius: 6px;
          }}
          code:not(pre code) {{
            background-color: #21262d !important;
            color: #ff7b72 !important;
            padding: 1px 4px;
            border-radius: 3px;
          }}
          table {{ border-color: #30363d !important; }}
          thead, th {{ background-color: #21262d !important; color: #e6edf3 !important; }}
          td {{ color: #c9d1d9 !important; }}
          tr:nth-child(even) {{ background-color: #161b22 !important; }}
          tr:nth-child(odd)  {{ background-color: #0d1117 !important; }}
          #toc-toggle {{
            background: none; border: 1px solid #30363d; border-radius: 4px;
            cursor: pointer; font-size: 0.8em; padding: 2px 8px;
            margin-bottom: 8px; display: block; color: #8b949e;
          }}
          #toc-toggle:hover {{ background: #21262d; }}
          .doc-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
            gap: 1.4rem; margin: 2rem 0;
          }}
          .doc-card {{
            background: #161b22; border: 1px solid #30363d;
            border-radius: 12px; padding: 1.4rem 1.6rem;
            transition: transform .15s, box-shadow .15s;
            display: flex; flex-direction: column; gap: .8rem;
          }}
          .doc-card:hover {{
            transform: translateY(-3px);
            box-shadow: 0 8px 24px rgba(0,0,0,.4);
            border-color: #8b949e;
          }}
          .doc-card-icon {{ font-size: 2rem; line-height: 1; }}
          .doc-card-title {{
            font-size: 1rem; font-weight: 600;
            color: #e6edf3; line-height: 1.4;
          }}
          .doc-card-link {{
            display: inline-block; margin-top: auto;
            padding: .35rem .9rem; background: #1f6feb;
            color: #fff !important; border-radius: 6px;
            font-size: .85em; text-decoration: none !important;
            width: fit-content;
          }}
          .doc-card-link:hover {{ background: #388bfd; }}
          .doc-card-keywords {{
            display: flex; flex-wrap: wrap; gap: 4px; margin: .4rem 0 .6rem;
          }}
          .kw-badge {{
            display: inline-block; background: #1c2333; color: #79c0ff;
            border-radius: 4px; padding: 2px 7px;
            font-size: .72em; font-weight: 500; white-space: nowrap;
          }}
          .index-hero {{
            background: linear-gradient(135deg, #1f6feb 0%, #0d1117 100%);
            color: white; border-radius: 14px;
            padding: 2.5rem 2rem; margin-bottom: 2rem;
            border: 1px solid #30363d;
          }}
          .index-hero h2 {{ color: white !important; margin: 0 0 .5rem; font-size: 1.7rem; border: none; }}
          .index-hero p  {{ color: rgba(255,255,255,.8); margin: 0; font-size: 1rem; }}
          footer, #quarto-footer {{
            background-color: #0d1117 !important;
            border-top: 1px solid #30363d; color: #8b949e !important;
          }}
        </style>
        <script>
        document.addEventListener('DOMContentLoaded', function () {{
          var toc = document.getElementById('TOC');
          if (!toc) return;
          var btn = document.createElement('button');
          btn.id = 'toc-toggle';
          btn.textContent = 'On this page ▲';
          btn.setAttribute('aria-expanded', 'true');
          btn.onclick = function () {{
            var expanded = btn.getAttribute('aria-expanded') === 'true';
            var inner = toc.querySelector('ul');
            if (inner) inner.style.display = expanded ? 'none' : '';
            btn.textContent = expanded ? 'On this page ▼' : 'On this page ▲';
            btn.setAttribute('aria-expanded', expanded ? 'false' : 'true');
          }};
          toc.insertBefore(btn, toc.firstChild);
        }});
        </script>
"""


def build_index_page(site_title: str, docs: list[tuple[str, str, list[str]]]) -> str:
    """
    docs: list of (qmd_filename, human_title, keywords)
    """
    cards_html = ""
    for qmd_file, title, keywords in docs:
        html_file  = qmd_file.replace(".qmd", ".html")
        badges_html = "".join(
            f'<span class="kw-badge">{kw}</span>' for kw in keywords
        )
        keywords_block = (
            f'<div class="doc-card-keywords">{badges_html}</div>'
            if badges_html else ""
        )
        cards_html += f"""
<div class="doc-card">
  <div class="doc-card-icon">📄</div>
  <div class="doc-card-title">{title}</div>
  {keywords_block}
  <a class="doc-card-link" href="{html_file}">Open →</a>
</div>"""

    return f"""---
title: "Document Library"
page-layout: full
toc: false
---

<div class="index-hero">
  <h2>📚 {site_title}</h2>
  <p>{len(docs)} document{"s" if len(docs) != 1 else ""} available</p>
</div>

<div class="doc-grid">
{cards_html}
</div>
"""


def process_directory(input_dir: str, output_dir: str | None = None) -> None:
    input_path = Path(input_dir).resolve()
    if not input_path.is_dir():
        print(f"Error: '{input_dir}' is not a valid directory.")
        sys.exit(1)

    output_path = Path(output_dir).resolve() if output_dir else input_path / "output"
    quarto_dir = output_path / "quarto"
    chunks_dir = output_path / "chunks"
    quarto_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)

    files = [f for f in input_path.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS]

    if not files:
        print(f"No supported files found in '{input_path}'.")
        print(f"Supported extensions: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        return

    print(f"{len(files)} file(s) found in '{input_path}'.\n")

    pdf_opts = PdfPipelineOptions()
    pdf_opts.generate_picture_images = True
    pdf_opts.images_scale = 2.0

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_opts),
        }
    )
    chunker = HierarchicalChunker()

    # (qmd_name, human_title, keywords)
    generated_docs: list[tuple[str, str, list[str]]] = []

    for file in files:
        print(f"Processing: {file.name}")
        try:
            result = converter.convert(str(file))

            # Export markdown with base64 images then extract to a folder
            markdown_embedded = result.document.export_to_markdown(image_mode=ImageRefMode.EMBEDDED)
            images_dir = quarto_dir / f"{file.stem}_files"
            markdown = strip_toc_and_summary(
                extract_images(markdown_embedded, images_dir, file.stem)
            )
            if images_dir.exists():
                n_images = len(list(images_dir.iterdir()))
                print(f"  -> Images: {n_images} image(s) -> {images_dir.relative_to(output_path.parent)}/")

            # Quarto page
            title = file.stem.replace("_", " ").replace("-", " ").title()
            quarto_content = build_quarto_page(title, markdown)
            qmd_path = quarto_dir / f"{file.stem}.qmd"
            qmd_path.write_text(quarto_content, encoding="utf-8")
            print(f"  -> Quarto : {qmd_path.relative_to(output_path.parent)}")

            # Hierarchical chunking — exclude TOC/summary sections
            chunks = list(chunker.chunk(result.document))
            all_chunks_data = [chunk_to_dict(c, i) for i, c in enumerate(chunks)]
            chunks_data = [c for c in all_chunks_data if not is_toc_chunk(c)]
            removed = len(all_chunks_data) - len(chunks_data)
            if removed:
                print(f"  -> TOC/summary: {removed} chunk(s) removed")
            json_path = chunks_dir / f"{file.stem}_chunks.json"
            json_path.write_text(json.dumps(chunks_data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  -> Chunks: {len(chunks)} chunk(s) -> {json_path.relative_to(output_path.parent)}")

            # Extract keywords from section headings
            seen_kw: set[str] = set()
            keywords: list[str] = []
            for chunk in chunks_data:
                for heading in chunk.get("metadata", {}).get("headings", []):
                    h = heading.strip()
                    if h and 3 <= len(h) <= 50 and h.lower() not in seen_kw:
                        seen_kw.add(h.lower())
                        keywords.append(h)
                        if len(keywords) == 8:
                            break
                if len(keywords) == 8:
                    break
            print(f"  -> Keywords: {', '.join(keywords) or '(none)'}")

            generated_docs.append((qmd_path.name, title, keywords))

        except Exception as exc:
            print(f"  /!\\ Error processing '{file.name}': {exc}")

    # Generate _quarto.yml and the index page
    if generated_docs:
        site_title = input_path.name.replace("_", " ").replace("-", " ").title()

        # Static assets (logo SVG)
        write_quarto_assets(quarto_dir)

        # _quarto.yml generation
        yml_content = build_quarto_yml(site_title)
        yml_path = quarto_dir / "_quarto.yml"
        yml_path.write_text(yml_content, encoding="utf-8")
        print(f"\n-> _quarto.yml generated: {yml_path.relative_to(output_path.parent)}")

        # index.qmd generation
        docs_sorted = sorted(generated_docs, key=lambda x: x[0])
        index_content = build_index_page(site_title, docs_sorted)
        index_path = quarto_dir / "index.qmd"
        index_path.write_text(index_content, encoding="utf-8")
        print(f"-> index.qmd generated: {index_path.relative_to(output_path.parent)}")

    print(f"\nDone. Results in '{output_path}'.")
    if generated_docs:
        print(f"To build the site: cd {quarto_dir} && quarto render")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python process_documents.py <source_directory> [output_directory]")
        sys.exit(1)

    input_dir = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None
    process_directory(input_dir, output_dir)
