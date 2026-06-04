#!/usr/bin/env python3
"""
CLI tool — ingest a directory of documents into the Dragon RAG system.

Mirrors the ingest pipeline of app_flask.py but runs entirely from the
command line, with no Flask dependency.

Usage:
    python ingest_dir.py <input_dir>
    python ingest_dir.py <input_dir> --output doc_output --db .chroma_db
    python ingest_dir.py <input_dir> --no-render
    python ingest_dir.py <input_dir> --force      # re-index already-indexed docs

Output layout (mirrors app_flask.py):
    <output>/quarto/<stem>.qmd
    <output>/quarto/<stem>_files/image_NNN.png
    <output>/quarto/_quarto.yml  (regenerated)
    <output>/quarto/index.qmd    (regenerated)
    <output>/quarto/_site/       (after quarto render)
    <output>/chunks/<stem>_chunks.json
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from docling.chunking import HierarchicalChunker
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import ImageRefMode

from process_documents import (
    SUPPORTED_EXTENSIONS,
    build_index_page,
    build_quarto_page,
    build_quarto_yml,
    chunk_to_dict,
    extract_images,
    is_toc_chunk,
    strip_toc_and_summary,
)

COLLECTION_NAME = "mes_docs"
EMBED_MODEL     = "all-MiniLM-L6-v2"


# ── ChromaDB ──────────────────────────────────────────────────────────────────

def get_collection(db_path: str):
    client   = chromadb.PersistentClient(path=db_path)
    embed_fn = SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embed_fn,
    )


# ── Quarto index ──────────────────────────────────────────────────────────────

def rebuild_quarto_index(quarto_dir: Path, chunks_dir: Path) -> None:
    qmd_files = sorted(
        f for f in quarto_dir.glob("*.qmd") if f.name != "index.qmd"
    )
    docs: list[tuple[str, str, list[str]]] = []
    for qmd in qmd_files:
        title      = qmd.stem.replace("_", " ").replace("-", " ").title()
        json_path  = chunks_dir / f"{qmd.stem}_chunks.json"
        keywords: list[str] = []
        if json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            seen: set[str] = set()
            for chunk in data:
                for h in chunk.get("metadata", {}).get("headings", []):
                    h = h.strip()
                    if h and 3 <= len(h) <= 50 and h.lower() not in seen:
                        seen.add(h.lower())
                        keywords.append(h)
                        if len(keywords) == 8:
                            break
                if len(keywords) == 8:
                    break
        docs.append((qmd.name, title, keywords))

    site_title = quarto_dir.parent.parent.name.replace("_", " ").replace("-", " ").title()
    (quarto_dir / "_quarto.yml").write_text(build_quarto_yml(site_title),         encoding="utf-8")
    (quarto_dir / "index.qmd" ).write_text(build_index_page(site_title, docs),   encoding="utf-8")
    print(f"  ✔ Index regenerated ({len(docs)} document(s))")


# ── Single-file pipeline ──────────────────────────────────────────────────────

def ingest_file(
    file_path:  Path,
    quarto_dir: Path,
    chunks_dir: Path,
    collection,
    force:      bool = False,
) -> bool:
    """Process one file. Returns True on success."""
    stem        = file_path.stem
    source_name = stem + "_chunks"

    if not force:
        existing = collection.get(where={"source": source_name})
        if existing["ids"]:
            print(f"  ⏭  {file_path.name} already indexed — skipping (use --force to re-index)")
            return True

    print(f"\n📄 {file_path.name}")

    # 1. Convert
    pdf_opts = PdfPipelineOptions()
    pdf_opts.generate_picture_images = True
    pdf_opts.images_scale = 2.0
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_opts)}
    )
    try:
        result = converter.convert(str(file_path))
    except Exception as e:
        print(f"  ❌ Conversion failed: {e}")
        return False
    print("  ✔ Converted")

    # 2. Markdown + images
    md_embedded = result.document.export_to_markdown(image_mode=ImageRefMode.EMBEDDED)
    images_dir  = quarto_dir / f"{stem}_files"
    markdown    = strip_toc_and_summary(
        extract_images(md_embedded, images_dir, stem)
    )
    n_images = len(list(images_dir.iterdir())) if images_dir.exists() else 0
    print(f"  ✔ Markdown exported ({n_images} image{'s' if n_images != 1 else ''})")

    # 3. Quarto page
    title    = stem.replace("_", " ").replace("-", " ").title()
    qmd_path = quarto_dir / f"{stem}.qmd"
    qmd_path.write_text(build_quarto_page(title, markdown), encoding="utf-8")
    print(f"  ✔ {qmd_path.name}")

    # 4. Chunks
    chunker         = HierarchicalChunker()
    chunks          = list(chunker.chunk(result.document))
    all_chunks_data = [chunk_to_dict(c, i) for i, c in enumerate(chunks)]
    chunks_data     = [c for c in all_chunks_data if not is_toc_chunk(c)]
    removed         = len(all_chunks_data) - len(chunks_data)
    if removed:
        print(f"  ↳ {removed} TOC chunk(s) removed")
    json_path = chunks_dir / f"{stem}_chunks.json"
    json_path.write_text(json.dumps(chunks_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✔ {len(chunks_data)} chunks saved")

    # 5. ChromaDB
    existing = collection.get(where={"source": source_name})
    if existing["ids"]:
        collection.delete(ids=existing["ids"])
        print(f"  ↳ Removed {len(existing['ids'])} old ChromaDB entries")

    texts, metas, ids = [], [], []
    for chunk in chunks_data:
        texts.append(chunk["text"])
        metas.append({
            "source":   source_name,
            "headings": json.dumps(chunk.get("metadata", {}).get("headings", [])),
        })
        ids.append(f"{source_name}_{chunk['index']}")
    if texts:
        collection.add(documents=texts, metadatas=metas, ids=ids)
    print(f"  ✔ {len(texts)} vectors indexed in ChromaDB")

    return True


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest a directory of documents into the Dragon RAG system.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
    )
    parser.add_argument("input_dir",
                        help="Directory containing documents to ingest")
    parser.add_argument("--output",  default=None,
                        help="Output directory (default: <script_dir>/doc_output)")
    parser.add_argument("--db",      default=None,
                        help="ChromaDB path (default: <script_dir>/.chroma_db)")
    parser.add_argument("--no-render", action="store_true",
                        help="Skip quarto render after ingest")
    parser.add_argument("--force",   action="store_true",
                        help="Re-index documents already present in ChromaDB")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    input_dir  = Path(args.input_dir).resolve()
    output_dir = Path(args.output).resolve() if args.output else script_dir / "doc_output"
    db_path    = Path(args.db).resolve()     if args.db     else script_dir / ".chroma_db"

    if not input_dir.is_dir():
        print(f"Error: '{input_dir}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    quarto_dir = output_dir / "quarto"
    chunks_dir = output_dir / "chunks"
    quarto_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)

    files = [
        f for f in sorted(input_dir.iterdir())
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    if not files:
        print(f"No supported files found in '{input_dir}'.")
        print(f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        sys.exit(0)

    print(f"Dragon ingest — {len(files)} file(s) in '{input_dir}'")
    print(f"  output : {output_dir}")
    print(f"  db     : {db_path}")

    collection = get_collection(str(db_path))

    ok, failed = 0, 0
    for f in files:
        if ingest_file(f, quarto_dir, chunks_dir, collection, force=args.force):
            ok += 1
        else:
            failed += 1

    print(f"\n── Index ────────────────────────────────")
    rebuild_quarto_index(quarto_dir, chunks_dir)

    if not args.no_render:
        print("\n── Quarto render ────────────────────────")
        try:
            result = subprocess.run(
                ["quarto", "render"],
                cwd=str(quarto_dir),
                timeout=600,
            )
            if result.returncode == 0:
                print(f"  ✔ Site generated → {quarto_dir / '_site'}")
            else:
                print("  ⚠️  quarto render failed — check output above")
        except FileNotFoundError:
            print("  ⚠️  quarto not found — run `quarto render` manually in:")
            print(f"      {quarto_dir}")
        except subprocess.TimeoutExpired:
            print("  ⚠️  quarto render timed out")

    print(f"\n{'✅' if not failed else '⚠️ '} Done — {ok} succeeded, {failed} failed")


if __name__ == "__main__":
    main()
