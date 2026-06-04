"""
Gradio interface for the document RAG system — conversation mode.
Automatically starts a local server for the Quarto docs.

Usage: python app_gradio.py
"""

import functools
import http.server
import json
import os
import re
import socketserver
import threading
from pathlib import Path

import anthropic
import chromadb
import gradio as gr
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from docling.document_converter import DocumentConverter

COLLECTION_NAME = "mes_docs"
EMBED_MODEL     = "all-MiniLM-L6-v2"
CLAUDE_MODEL    = "claude-sonnet-4-6"
N_RESULTS       = 5
CONFIG_FILE     = ".rag_config.json"
DOCS_PORT       = 8080

chroma   = chromadb.PersistentClient(path=".chroma_db")
embed_fn = SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    p = Path(CONFIG_FILE)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_config(data: dict) -> None:
    config = load_config()
    config.update(data)
    Path(CONFIG_FILE).write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── API key ───────────────────────────────────────────────────────────────────

def load_saved_api_key() -> str:
    return load_config().get("anthropic_api_key", "")


def save_api_key(key: str) -> str:
    key = key.strip()
    if not key:
        return "⚠️ Please enter a valid API key."
    if not key.startswith("sk-ant-"):
        return "⚠️ This does not look like a valid Anthropic key (should start with `sk-ant-`)."
    save_config({"anthropic_api_key": key})
    os.environ["ANTHROPIC_API_KEY"] = key
    return "✔ API key saved and active."


def api_key_status() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        masked = key[:12] + "..." + key[-4:]
        return f"🟢 Key active: `{masked}`"
    return "🔴 No API key — go to the **⚙️ Settings** tab to add one."


# ── ChromaDB ──────────────────────────────────────────────────────────────────

def get_collection():
    return chroma.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embed_fn,
    )


# ── Utils ─────────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


def start_docs_server(site_dir: Path, port: int) -> bool:
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(site_dir),
    )
    try:
        httpd = socketserver.TCPServer(("", port), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"Documentation available at http://localhost:{port}/")
        return True
    except OSError:
        print(f"Port {port} already in use — docs server not started.")
        return False


# ── RAG pipeline ──────────────────────────────────────────────────────────────

def translate_question(client: anthropic.Anthropic, question: str) -> tuple[str, str]:
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                "Translate the following question into English and French. "
                "Return ONLY a valid JSON object with exactly two keys: \"en\" and \"fr\". "
                "No explanation, no markdown, just the JSON.\n\n"
                f"Question: {question}"
            ),
        }],
    )
    data = json.loads(msg.content[0].text)
    return data["en"], data["fr"]


def merge_results(res_a: dict, res_b: dict) -> tuple[list, list, list]:
    seen_ids: set[str] = set()
    ids, docs, metas = [], [], []
    for chunk_ids, chunk_docs, chunk_metas in [
        (res_a["ids"][0], res_a["documents"][0], res_a["metadatas"][0]),
        (res_b["ids"][0], res_b["documents"][0], res_b["metadatas"][0]),
    ]:
        for cid, doc, meta in zip(chunk_ids, chunk_docs, chunk_metas):
            if cid not in seen_ids:
                seen_ids.add(cid)
                ids.append(cid)
                docs.append(doc)
                metas.append(meta)
    return ids, docs, metas


def build_refs_html(metas: list) -> str:
    docs_base = f"http://localhost:{DOCS_PORT}"

    seen, items = set(), []
    for meta in metas:
        src      = meta["source"]
        headings = json.loads(meta.get("headings", "[]"))
        anchor   = f"#{slugify(headings[-1])}" if headings else ""
        ref_key  = f"{src}{anchor}"
        if ref_key in seen:
            continue
        seen.add(ref_key)

        base_name = src.removesuffix("_chunks")
        url       = f"{docs_base}/{base_name}.html{anchor}"
        label     = " › ".join(headings) if headings else base_name
        items.append(
            f'<li style="margin:5px 0">'
            f'<a href="{url}" target="_blank" '
            f'style="color:#1a73e8;text-decoration:none;font-size:0.9em">'
            f'📄 {label}</a></li>'
        )

    if not items:
        return ""
    return (
        '<div style="margin-top:12px;padding:10px 14px;'
        'background:#f8f9fa;border-radius:8px;border:1px solid #e0e0e0">'
        '<p style="margin:0 0 6px;font-weight:600;font-size:0.9em">References:</p>'
        f'<ul style="margin:0;padding-left:16px">{"".join(items)}</ul>'
        "</div>"
    )


def process_uploaded_file(file) -> tuple[str, str]:
    """Convert an uploaded file to text. Returns (status_message, extracted_text)."""
    if file is None:
        return "", ""
    file_path = file if isinstance(file, str) else file.name
    try:
        converter = DocumentConverter()
        result    = converter.convert(file_path)
        text      = result.document.export_to_markdown()
        name      = Path(file_path).name
        return f"✔ **{name}** loaded ({len(text):,} characters)", text
    except Exception as e:
        return f"⚠️ Error processing file: {e}", ""


def chat(
    message: str,
    history: list,
    uploaded_doc: str,
) -> tuple[list, str, str, str]:
    """
    Returns: (updated_history, cleared_input, translations_md, refs_html)
    """
    if not message.strip():
        return history, "", "", ""

    if not os.environ.get("ANTHROPIC_API_KEY"):
        new_history = history + [
            {"role": "user",      "content": message},
            {"role": "assistant", "content": "**Error**: no API key set.\n\nGo to the **⚙️ Settings** tab to add your Anthropic API key."},
        ]
        return new_history, "", "", ""

    client = anthropic.Anthropic()

    # Translate question
    try:
        question_en, question_fr = translate_question(client, message)
    except Exception as e:
        new_history = history + [
            {"role": "user",      "content": message},
            {"role": "assistant", "content": f"**Translation error**: {e}"},
        ]
        return new_history, "", "", ""

    translations_md = (
        f"**Queries sent to the index:**\n"
        f"- 🇬🇧 {question_en}\n"
        f"- 🇫🇷 {question_fr}"
    )

    # Retrieve relevant chunks
    collection = get_collection()
    results_en = collection.query(query_texts=[question_en], n_results=N_RESULTS)
    results_fr = collection.query(query_texts=[question_fr], n_results=N_RESULTS)
    _, docs, metas = merge_results(results_en, results_fr)
    sources = [m["source"] for m in metas]

    MAX_CHARS = 2000
    context = "\n\n---\n\n".join(
        f"[Source: {src}]\n{doc[:MAX_CHARS]}"
        for src, doc in zip(sources, docs)
    )

    # System prompt: RAG context + optional uploaded document
    MAX_UPLOAD_CHARS = 30_000
    system_parts = [
        "You are a helpful assistant specialized in the provided documents. "
        "Answer questions based on the sources below. "
        "If the answer is not found in any source, say so clearly. "
        "Keep a natural conversation flow and remember previous messages.",
    ]
    if context:
        system_parts.append(f"\n## Knowledge base excerpts\n\n{context}")
    if uploaded_doc:
        system_parts.append(
            f"\n## Uploaded document (priority source)\n\n{uploaded_doc[:MAX_UPLOAD_CHARS]}"
        )
    system_prompt = "\n".join(system_parts)

    # History is already in Claude messages format
    claude_messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in history
    ]
    claude_messages.append({"role": "user", "content": message})

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=claude_messages,
        )
        answer = response.content[0].text
    except anthropic.AuthenticationError:
        answer = "**Error**: invalid or expired API key.\n\nGo to **⚙️ Settings** to update it."
    except Exception as e:
        answer = f"**Error**: {e}"

    new_history = history + [
        {"role": "user",      "content": message},
        {"role": "assistant", "content": answer},
    ]

    return new_history, "", translations_md, build_refs_html(metas)


# ── Documentation tab ────────────────────────────────────────────────────────

def get_docs_html() -> str:
    config = load_config()
    site_path = Path(config.get("quarto_site", ""))
    base_url = f"http://localhost:{DOCS_PORT}"

    if not site_path.exists():
        return (
            '<div style="padding:24px;text-align:center;color:#6c757d;'
            'border:1px dashed #dee2e6;border-radius:8px">'
            '<p style="font-size:1.1em">⚠️ No documentation found</p>'
            '<p>Run <code>process_documents.py</code> then <code>quarto render</code> '
            'to generate the documentation.</p></div>'
        )

    html_files = sorted(f for f in site_path.glob("*.html") if f.name != "index.html")

    cards = ""
    for f in html_files:
        title = f.stem.replace("_", " ").replace("-", " ").title()
        url = f"{base_url}/{f.name}"
        cards += (
            f'<div style="background:#fff;border:1px solid #e3e8ef;border-radius:10px;'
            f'padding:.9rem 1.2rem;display:flex;justify-content:space-between;align-items:center">'
            f'<span style="font-weight:500">📄 {title}</span>'
            f'<a href="{url}" target="_blank" style="padding:4px 12px;background:#375a7f;'
            f'color:white;border-radius:5px;text-decoration:none;font-size:.85em">Open ↗</a>'
            f'</div>'
        )

    if not cards:
        cards = (
            '<p style="color:#6c757d">No HTML documents found. '
            'Run <code>quarto render</code> in the quarto output directory.</p>'
        )

    return (
        f'<div style="margin-bottom:14px;display:flex;align-items:center;gap:12px">'
        f'<a href="{base_url}/" target="_blank" style="padding:6px 16px;background:#375a7f;'
        f'color:white;border-radius:6px;text-decoration:none">🏠 Open index ↗</a>'
        f'<span style="color:#6c757d;font-size:.9em">http://localhost:{DOCS_PORT}/</span>'
        f'</div>'
        f'<div style="display:flex;flex-direction:column;gap:8px">{cards}</div>'
    )


# ── Gradio Interface ──────────────────────────────────────────────────────────

with gr.Blocks(title="Drag and Rag") as demo:
    gr.Markdown("# Drag and Rag")

    with gr.Tabs():

        # ── Tab 1 : Chat ──────────────────────────────────────────────────────
        with gr.Tab("💬 Chat"):
            key_status = gr.Markdown(value=api_key_status)
            uploaded_doc_state = gr.State("")

            chatbot = gr.Chatbot(
                label="Conversation",
                height=460,
                show_label=False,
            )

            with gr.Row():
                msg_input = gr.Textbox(
                    placeholder="Ask a question about your documents…",
                    lines=1,
                    scale=6,
                    show_label=False,
                    container=False,
                )
                send_btn  = gr.Button("Send",    variant="primary",   scale=1)
                clear_btn = gr.Button("🗑️ Clear", variant="secondary", scale=1)

            with gr.Accordion("📎 Add a document to the conversation", open=False):
                gr.Markdown(
                    "Upload a file to analyse it alongside the knowledge base. "
                    "Supported: PDF, DOCX, PPTX, HTML, MD, XLSX."
                )
                with gr.Row():
                    file_input = gr.File(
                        label="Upload a document",
                        file_types=[".pdf", ".docx", ".pptx", ".html", ".md", ".xlsx"],
                        scale=4,
                    )
                    remove_btn = gr.Button("✖ Remove", variant="secondary", scale=1)
                file_status = gr.Markdown()

                file_input.change(
                    fn=process_uploaded_file,
                    inputs=[file_input],
                    outputs=[file_status, uploaded_doc_state],
                )
                remove_btn.click(
                    fn=lambda: (None, "", ""),
                    outputs=[file_input, file_status, uploaded_doc_state],
                    queue=False,
                )

            with gr.Accordion("🔎 Translated queries", open=False):
                translations_output = gr.Markdown()

            refs_output = gr.HTML()

            send_btn.click(
                fn=chat,
                inputs=[msg_input, chatbot, uploaded_doc_state],
                outputs=[chatbot, msg_input, translations_output, refs_output],
            )
            msg_input.submit(
                fn=chat,
                inputs=[msg_input, chatbot, uploaded_doc_state],
                outputs=[chatbot, msg_input, translations_output, refs_output],
            )
            clear_btn.click(
                fn=lambda: ([], "", "", ""),
                outputs=[chatbot, msg_input, translations_output, refs_output],
                queue=False,
            )

        # ── Tab 2 : Documentation ─────────────────────────────────────────────
        with gr.Tab("📚 Documentation"):
            docs_output = gr.HTML(value=get_docs_html())
            refresh_btn = gr.Button("🔄 Refresh", variant="secondary", size="sm")
            refresh_btn.click(fn=get_docs_html, inputs=[], outputs=[docs_output])

        # ── Tab 3 : Settings ──────────────────────────────────────────────────
        with gr.Tab("⚙️ Settings"):
            gr.Markdown("### Claude API Key")
            gr.Markdown(
                "Your key is stored locally in `.rag_config.json` and never sent anywhere "
                "other than the Anthropic API. "
                "Get a key at [console.anthropic.com](https://console.anthropic.com/settings/keys)."
            )

            with gr.Row():
                api_key_input = gr.Textbox(
                    label="Anthropic API Key",
                    type="password",
                    placeholder="sk-ant-...",
                    value=load_saved_api_key,
                    scale=4,
                )
                save_key_btn = gr.Button("Save", variant="primary", scale=1)

            save_status = gr.Markdown()

            def on_save(key: str):
                msg = save_api_key(key)
                return msg, api_key_status()

            save_key_btn.click(
                fn=on_save,
                inputs=[api_key_input],
                outputs=[save_status, key_status],
            )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    saved_key = load_saved_api_key()
    if saved_key:
        os.environ["ANTHROPIC_API_KEY"] = saved_key
        print("API key loaded from config.")

    config      = load_config()
    quarto_site = Path(config.get("quarto_site", ""))
    if quarto_site.exists():
        start_docs_server(quarto_site, DOCS_PORT)
    else:
        print(f"Warning: Quarto site not found ({quarto_site or 'path not configured'}).")
        print("Run first:")
        print("  python process_documents.py <input_dir> <output_dir>")
        print("  python embed_and_query.py index <chunks_dir>")

    demo.launch(theme=gr.themes.Monochrome())
