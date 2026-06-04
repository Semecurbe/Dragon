"""
Indexes JSON chunks into ChromaDB (local embeddings via sentence-transformers)
and allows querying with Claude as the answering LLM.

Usage:
  Index : python embed_and_query.py index <chunks_folder>
  Query : python embed_and_query.py query "<your question>"
"""

import json
import os
import re
import sys
from pathlib import Path

import anthropic
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

COLLECTION_NAME = "mes_docs"
EMBED_MODEL = "all-MiniLM-L6-v2"
CLAUDE_MODEL = "claude-sonnet-4-6"
N_RESULTS = 5
CONFIG_FILE = ".rag_config.json"

chroma = chromadb.PersistentClient(path=".chroma_db")
embed_fn = SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)


def get_collection():
    return chroma.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embed_fn,
    )


def load_config() -> dict:
    p = Path(CONFIG_FILE)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_config(data: dict) -> None:
    config = load_config()
    config.update(data)
    Path(CONFIG_FILE).write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def slugify(text: str) -> str:
    """Convert a heading to an HTML anchor (same logic as Pandoc/Quarto)."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


def build_html_link(source: str, headings: list[str], quarto_site: Path) -> str:
    base_name = source.removesuffix("_chunks")
    html_file = quarto_site / f"{base_name}.html"
    anchor = f"#{slugify(headings[-1])}" if headings else ""
    return f"file://{html_file}{anchor}"


def index_chunks(chunks_dir: str) -> None:
    chunks_path = Path(chunks_dir)
    json_files = list(chunks_path.glob("*_chunks.json"))

    if not json_files:
        print(f"No *_chunks.json file found in '{chunks_dir}'.")
        sys.exit(1)

    # Save the path to the Quarto site for links
    quarto_site = (chunks_path.parent / "quarto" / "_site").resolve()
    save_config({"quarto_site": str(quarto_site)})

    collection = get_collection()
    total = 0

    for json_file in json_files:
        chunks = json.loads(json_file.read_text(encoding="utf-8"))
        ids, texts, metadatas = [], [], []

        for chunk in chunks:
            chunk_id = f"{json_file.stem}__{chunk['index']}"
            text = chunk.get("text", "").strip()
            if not text:
                continue
            headings = chunk.get("metadata", {}).get("headings", [])
            ids.append(chunk_id)
            texts.append(text)
            metadatas.append({
                "source": json_file.stem,
                "index": chunk["index"],
                "headings": json.dumps(headings),
            })

        if ids:
            collection.upsert(ids=ids, documents=texts, metadatas=metadatas)
            total += len(ids)
            print(f"  {json_file.name} → {len(ids)} chunks indexed")

    print(f"\nTotal: {total} chunks in collection '{COLLECTION_NAME}'.")


def query(question: str) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: the ANTHROPIC_API_KEY environment variable is not set.")
        print("  export ANTHROPIC_API_KEY=\"sk-ant-...\"")
        sys.exit(1)

    collection = get_collection()
    results = collection.query(query_texts=[question], n_results=N_RESULTS)
    docs = results["documents"][0]
    metas = results["metadatas"][0]
    sources = [m["source"] for m in metas]

    MAX_CHARS_PER_CHUNK = 2000
    context = "\n\n---\n\n".join(
        f"[Source: {src}]\n{doc[:MAX_CHARS_PER_CHUNK]}"
        for src, doc in zip(sources, docs)
    )

    client = anthropic.Anthropic()
    try:
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Here are document excerpts:\n\n{context}\n\n"
                        f"Question: {question}\n\n"
                        "Answer based only on the provided excerpts. "
                        "If the answer is not in the excerpts, say so clearly."
                    ),
                }
            ],
        )
    except anthropic.BadRequestError as e:
        print(f"Claude API error (400)")
        print(f"  body    : {e.body}")
        print(f"  str     : {str(e)}")
        print(f"  response: {e.response.text if e.response else 'N/A'}")
        sys.exit(1)

    print("\nClaude's response:\n")
    print(message.content[0].text)

    # References with links to the Quarto HTML pages
    config = load_config()
    quarto_site = Path(config.get("quarto_site", ""))

    print("\nReferences:")
    seen = set()
    for meta in metas:
        src = meta["source"]
        headings = json.loads(meta.get("headings", "[]"))
        anchor = f"#{slugify(headings[-1])}" if headings else ""
        ref_key = f"{src}{anchor}"

        if ref_key in seen:
            continue
        seen.add(ref_key)

        heading_display = " > ".join(headings) if headings else src.removesuffix("_chunks")
        link = build_html_link(src, headings, quarto_site)
        print(f"  • {heading_display}")
        print(f"    {link}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage:")
        print("  python embed_and_query.py index <chunks_folder>")
        print('  python embed_and_query.py query "<your question>"')
        sys.exit(1)

    command = sys.argv[1]

    if command == "index":
        index_chunks(sys.argv[2])
    elif command == "query":
        query(sys.argv[2])
    else:
        print(f"Unknown command: '{command}'. Use 'index' or 'query'.")
        sys.exit(1)
