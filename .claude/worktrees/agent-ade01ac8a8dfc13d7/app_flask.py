"""
Flask interface for the document RAG system.
Replaces app_gradio.py — navigation via classic HTML routes.

Usage: python app_flask.py
"""

import functools
import http.server
import json
import os
import queue
import re
import shutil
import socket
import socketserver
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import anthropic
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from docling.chunking import HierarchicalChunker
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import ImageRefMode
from flask import Flask, Response, jsonify, redirect, render_template, request, session, stream_with_context
from process_documents import (
    build_index_page, build_quarto_page, build_quarto_yml,
    chunk_to_dict, extract_images, strip_toc_and_summary, is_toc_chunk,
    write_quarto_assets,
)

COLLECTION_NAME       = "mes_docs"
EMBED_MODEL           = "all-MiniLM-L6-v2"
CLAUDE_MODEL          = "claude-sonnet-4-6"
N_RESULTS             = 5
CONFIG_FILE           = ".rag_config.json"
DOCS_PORT             = 8080
APP_PORT              = 7860
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant specialized in the provided documents. "
    "Answer questions based on the sources below. "
    "If the answer is not found in any source, say so clearly. "
    "Keep a natural conversation flow and remember previous messages."
)

# Directory where app_flask.py lives (used to resolve relative paths)
APP_DIR = Path(__file__).parent


def _get_local_ip() -> str:
    """Return the machine's LAN IP (used to expose docs/app to the network)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


HOST_IP   = _get_local_ip()
DOCS_BASE = f"http://{HOST_IP}:{DOCS_PORT}"
APP_BASE  = f"http://{HOST_IP}:{APP_PORT}"

chroma   = chromadb.PersistentClient(path=".chroma_db")
embed_fn = SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)

app = Flask(__name__)
app.secret_key = os.urandom(24)


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


def _resolve_site(raw: str) -> Path:
    """Resolve quarto_site: relative path → relative to APP_DIR, absolute → as-is."""
    p = Path(raw) if raw else Path("")
    if p.parts and not p.is_absolute():
        p = APP_DIR / p
    return p


def load_saved_api_key() -> str:
    return load_config().get("anthropic_api_key", "")


def save_api_key(key: str) -> tuple[str, str]:
    key = key.strip()
    if not key:
        return "danger", "Please enter a valid API key."
    if not key.startswith("sk-ant-"):
        return "warning", "This does not look like a valid Anthropic key (should start with sk-ant-)."
    save_config({"anthropic_api_key": key})
    os.environ["ANTHROPIC_API_KEY"] = key
    return "success", "API key saved and active."


def api_key_status() -> tuple[str, str]:
    config   = load_config()
    provider = config.get("llm_provider", "claude")
    if provider == "ollama":
        model = config.get("ollama_model", "llama3.2")
        url   = config.get("ollama_url",   "http://localhost:11434")
        return "success", f"🦙 Ollama local — {model} ({url})"
    # Claude
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        masked = key[:12] + "..." + key[-4:]
        return "success", f"🤖 Claude API active: {masked}"
    return "danger", "No API key — go to Settings to add one."


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


class _NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    """Docs server handler — force no-cache so Chrome always fetches fresh content."""

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format, *args):  # silence access logs
        pass


def start_docs_server(site_dir: Path, port: int) -> bool:
    handler = functools.partial(_NoCacheHandler, directory=str(site_dir))
    try:
        httpd = socketserver.TCPServer(("", port), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"Documentation available at http://{HOST_IP}:{port}/")
        return True
    except OSError:
        print(f"Port {port} already in use — docs server not started.")
        return False


# ── LLM abstraction (Claude or Ollama) ───────────────────────────────────────

def _ollama_chat(messages: list[dict], system: str, url: str, model: str) -> str:
    """Call Ollama /api/chat and return the assistant reply."""
    payload_messages: list[dict] = []
    if system:
        payload_messages.append({"role": "system", "content": system})
    payload_messages.extend(messages)

    payload = json.dumps({
        "model":    model,
        "messages": payload_messages,
        "stream":   False,
    }).encode()

    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        return data["message"]["content"]
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Ollama unreachable ({url}) — make sure the service is running: {e}"
        ) from e


def _llm_chat(
    messages: list[dict],
    *,
    system: str = "",
    max_tokens: int = 2048,
) -> str:
    """Unified LLM call — routes to Claude or Ollama depending on config."""
    config   = load_config()
    provider = config.get("llm_provider", "claude")

    if provider == "ollama":
        url   = config.get("ollama_url",   "http://localhost:11434")
        model = config.get("ollama_model", "llama3.2")
        return _ollama_chat(messages, system=system, url=url, model=model)

    # ── Claude (default) ─────────────────────────────────────────────────────
    client = anthropic.Anthropic()
    kwargs: dict = dict(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        messages=messages,
    )
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    return resp.content[0].text


# ── RAG pipeline ──────────────────────────────────────────────────────────────

def translate_question(question: str) -> tuple[str, str]:
    """Translate a question to EN + FR via the configured LLM."""
    text = _llm_chat(
        messages=[{
            "role": "user",
            "content": (
                "Translate the following question into English and French. "
                "Return ONLY a valid JSON object with exactly two keys: \"en\" and \"fr\". "
                "No explanation, no markdown, just the JSON.\n\n"
                f"Question: {question}"
            ),
        }],
        max_tokens=256,
    )
    # Strip potential markdown code fences from local models
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    try:
        data = json.loads(text)
        return data["en"], data["fr"]
    except (json.JSONDecodeError, KeyError):
        # Graceful fallback: use original question for both languages
        return question, question


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


def _fetch_neighbor_chunks(
    collection, matched_ids: list[str]
) -> tuple[list[str], list[dict]]:
    """Fetch the 2 chunks before and after each matched chunk for richer context.

    Chunk IDs have the form "{source}_{index}" where source itself may contain
    underscores, so we use rsplit("_", 1) to peel off only the trailing index.
    """
    candidates: list[str] = []
    seen = set(matched_ids)
    for chunk_id in matched_ids:
        parts = chunk_id.rsplit("_", 1)
        if len(parts) != 2:
            continue
        source, idx_str = parts
        try:
            idx = int(idx_str)
        except ValueError:
            continue
        for offset in (-2, -1, 1, 2):
            n = idx + offset
            if n >= 0:
                nid = f"{source}_{n}"
                if nid not in seen:
                    candidates.append(nid)
                    seen.add(nid)
    if not candidates:
        return [], []
    try:
        result = collection.get(ids=candidates, include=["documents", "metadatas"])
        return result["documents"], result["metadatas"]
    except Exception:
        return [], []


def build_refs_html(metas: list) -> str:
    docs_base = DOCS_BASE
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
            f'<li><a href="{url}" target="_blank" class="text-decoration-none">'
            f'📄 {label}</a></li>'
        )
    if not items:
        return ""
    return (
        '<div class="mt-3 p-3 bg-light rounded border">'
        '<p class="mb-1 fw-semibold small">References:</p>'
        f'<ul class="mb-0 ps-3 small">{"".join(items)}</ul>'
        '</div>'
    )


# ── Ingest pipeline ──────────────────────────────────────────────────────────

_jobs: dict[str, queue.Queue] = {}

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".html", ".md", ".adoc", ".xlsx"}


def _get_output_dirs() -> tuple[Path, Path]:
    """Return (quarto_dir, chunks_dir) from config."""
    config = load_config()
    site = _resolve_site(config.get("quarto_site", ""))
    if site.parts and site.exists():
        quarto_dir = site.parent          # …/quarto/
        output_dir = quarto_dir.parent    # …/doc_output/
    else:
        output_dir = APP_DIR / "doc_output"
        quarto_dir = output_dir / "quarto"
    chunks_dir = output_dir / "chunks"
    quarto_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)
    return quarto_dir, chunks_dir


def _get_site_path() -> Path:
    """Return the Quarto _site directory (from config or computed default)."""
    config = load_config()
    site = _resolve_site(config.get("quarto_site", ""))
    if site.parts and site.exists():
        return site
    quarto_dir, _ = _get_output_dirs()
    return quarto_dir / "_site"


def _rebuild_quarto_index(quarto_dir: Path, chunks_dir: Path) -> None:
    """Regenerate _quarto.yml, index.qmd and static assets from all existing .qmd files."""
    write_quarto_assets(quarto_dir)
    qmd_files = sorted(
        f for f in quarto_dir.glob("*.qmd") if f.name != "index.qmd"
    )
    docs: list[tuple[str, str, list[str]]] = []
    for qmd in qmd_files:
        title = qmd.stem.replace("_", " ").replace("-", " ").title()
        # Extract keywords from matching chunks file
        json_path = chunks_dir / f"{qmd.stem}_chunks.json"
        keywords: list[str] = []
        if json_path.exists():
            chunks_data = json.loads(json_path.read_text(encoding="utf-8"))
            seen: set[str] = set()
            for chunk in chunks_data:
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
    (quarto_dir / "_quarto.yml").write_text(
        build_quarto_yml(site_title, app_url=APP_BASE), encoding="utf-8"
    )
    (quarto_dir / "index.qmd").write_text(
        build_index_page(site_title, docs), encoding="utf-8"
    )


def _process_file_task(file_path: Path, job_id: str) -> None:
    """Run the full ingest pipeline in a background thread."""
    q = _jobs[job_id]

    def put(msg: str, done: bool = False, error: bool = False) -> None:
        q.put({"msg": msg, "done": done, "error": error})

    try:
        if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            put(f"❌ Unsupported format: {file_path.suffix}", done=True, error=True)
            return

        quarto_dir, chunks_dir = _get_output_dirs()
        put(f"📄 Processing <strong>{file_path.name}</strong>…")

        # ── 1. Convert ────────────────────────────────────────────────────────
        pdf_opts = PdfPipelineOptions()
        pdf_opts.generate_picture_images = True
        pdf_opts.images_scale = 2.0
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_opts)}
        )
        result = converter.convert(str(file_path))
        put("✔ Document converted")

        # ── 2. Markdown + image extraction ────────────────────────────────────
        md_embedded = result.document.export_to_markdown(image_mode=ImageRefMode.EMBEDDED)
        images_dir  = quarto_dir / f"{file_path.stem}_files"
        markdown    = strip_toc_and_summary(
            extract_images(md_embedded, images_dir, file_path.stem)
        )
        n_images    = len(list(images_dir.iterdir())) if images_dir.exists() else 0
        put(f"✔ Markdown exported ({n_images} image{'s' if n_images != 1 else ''})")

        # ── 3. Quarto page ────────────────────────────────────────────────────
        title   = file_path.stem.replace("_", " ").replace("-", " ").title()
        qmd_path = quarto_dir / f"{file_path.stem}.qmd"
        qmd_path.write_text(build_quarto_page(title, markdown), encoding="utf-8")
        put(f"✔ Quarto page created: <code>{qmd_path.name}</code>")

        # ── 4. Chunking ───────────────────────────────────────────────────────
        chunker         = HierarchicalChunker()
        chunks          = list(chunker.chunk(result.document))
        all_chunks_data = [chunk_to_dict(c, i) for i, c in enumerate(chunks)]
        chunks_data     = [c for c in all_chunks_data if not is_toc_chunk(c)]
        removed         = len(all_chunks_data) - len(chunks_data)
        if removed:
            put(f"  ↳ {removed} TOC chunk(s) removed")
        json_path   = chunks_dir / f"{file_path.stem}_chunks.json"
        json_path.write_text(
            json.dumps(chunks_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        put(f"✔ {len(chunks)} chunks saved")

        # ── 5. Index in ChromaDB ──────────────────────────────────────────────
        collection  = get_collection()
        source_name = file_path.stem + "_chunks"
        existing    = collection.get(where={"source": source_name})
        if existing["ids"]:
            collection.delete(ids=existing["ids"])
            put(f"  ↳ Removed {len(existing['ids'])} old entries")

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
        put(f"✔ Indexed {len(texts)} chunks in ChromaDB")

        # ── 6. Rebuild Quarto index ───────────────────────────────────────────
        _rebuild_quarto_index(quarto_dir, chunks_dir)
        put("✔ Documentation index regenerated")

        # ── 7. Quarto render ──────────────────────────────────────────────────
        put("🔄 Running <code>quarto render</code>…")
        try:
            qr = subprocess.run(
                ["quarto", "render"],
                cwd=str(quarto_dir),
                capture_output=True, text=True, timeout=300,
            )
            if qr.returncode == 0:
                put("✔ Quarto render completed — documentation updated")
                start_docs_server(quarto_dir / "_site", DOCS_PORT)
            else:
                err = (qr.stderr or qr.stdout)[:300]
                put(f"⚠️ Quarto render error: <pre>{err}</pre>")
        except FileNotFoundError:
            put("⚠️ <code>quarto</code> not found — run <code>quarto render</code> manually")
        except subprocess.TimeoutExpired:
            put("⚠️ Quarto render timed out — run manually")

        put("✅ Done! The document is now available.", done=True)

    except Exception as exc:
        put(f"❌ Unexpected error: {exc}", done=True, error=True)


# ── PWA routes ───────────────────────────────────────────────────────────────

@app.route("/manifest.json")
def pwa_manifest():
    return jsonify({
        "name": "Dragon — Drag and Rag On",
        "short_name": "Dragon",
        "description": "Local RAG application — drag & drop documents, query with AI",
        "start_url": "/chat",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0d1117",
        "theme_color": "#0d1117",
        "lang": "en",
        "icons": [
            {"src": "/static/dragon-favicon.svg", "sizes": "any",
             "type": "image/svg+xml", "purpose": "any maskable"},
        ],
        "categories": ["productivity", "utilities"],
    })


@app.route("/icon/<int:size>")
def pwa_icon(size: int):
    """Dynamically generated SVG icon — document + magnifying glass."""
    # Clamp to valid sizes
    size = max(32, min(size, 512))
    s = 100  # internal viewBox coordinates
    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="{size}" height="{size}" viewBox="0 0 {s} {s}">
  <!-- Background rounded square -->
  <rect width="{s}" height="{s}" rx="22" fill="#375a7f"/>
  <!-- Document (white page) -->
  <rect x="20" y="12" width="44" height="57" rx="4" fill="white"/>
  <!-- Text lines on document -->
  <rect x="28" y="22" width="28" height="4" rx="2" fill="#c8d8ec"/>
  <rect x="28" y="31" width="28" height="4" rx="2" fill="#c8d8ec"/>
  <rect x="28" y="40" width="20" height="4" rx="2" fill="#c8d8ec"/>
  <!-- Magnifying glass circle -->
  <circle cx="62" cy="67" r="15" fill="none"
          stroke="#375a7f" stroke-width="6"/>
  <!-- Glass inner (white fill so it stands out) -->
  <circle cx="62" cy="67" r="8" fill="white"/>
  <!-- Handle -->
  <line x1="73" y1="78" x2="83" y2="88"
        stroke="#375a7f" stroke-width="7" stroke-linecap="round"/>
</svg>"""
    return Response(
        svg,
        mimetype="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.route("/sw.js")
def service_worker():
    """Minimal service worker: network-first, caches pages for offline fallback."""
    sw = """const CACHE = 'drag-and-rag-v1';
// Only cache truly static shell pages (not dynamic content like /docs)
const SHELL = ['/chat', '/ingest', '/settings'];
// These routes always fetch from network — never cache
const NO_CACHE = ['/docs', '/qmd', '/summary'];

// Pre-cache the app shell on install
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  // Remove old caches
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  e.waitUntil(clients.claim());
});

self.addEventListener('fetch', (e) => {
  // Skip non-GET and streaming API endpoints
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/')) return;

  // Dynamic routes: always go to network, never serve from cache
  const isDynamic = NO_CACHE.some((p) => url.pathname.startsWith(p));
  if (isDynamic) {
    e.respondWith(fetch(e.request));
    return;
  }

  // Network-first: try network, fall back to cache for shell pages
  e.respondWith(
    fetch(e.request)
      .then((response) => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
        }
        return response;
      })
      .catch(() => caches.match(e.request))
  );
});
"""
    return Response(
        sw,
        mimetype="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/about")
def about_page():
    return render_template("about.html", active="about")


@app.route("/")
def index():
    return redirect("/chat")


@app.route("/chat")
def chat_page():
    status_type, status_msg = api_key_status()
    status_color = {"success": "#198754", "danger": "#dc3545"}.get(status_type, "#6c757d")
    return render_template("chat.html", active="chat",
                           status_color=status_color, status_msg=status_msg)


@app.route("/docs")
def docs_page():
    site_path = _get_site_path()
    base_url  = DOCS_BASE

    if not site_path.exists():
        return render_template("docs.html", active="docs",
                               site_exists=False, base_url=base_url, doc_cards=[])

    html_files = sorted(
        f for f in site_path.glob("*.html") if f.name != "index.html"
    )
    _, chunks_dir = _get_output_dirs()
    summaries_dir = chunks_dir.parent / "summaries"

    doc_cards = []
    for f in html_files:
        title       = f.stem.replace("_", " ").replace("-", " ").title()
        doc_cards.append({
            "stem":        f.stem,
            "stem_js":     f.stem.replace("'", "\\'"),
            "title":       title,
            "title_js":    title.replace("'", "\\'"),
            "url":         f"{base_url}/{f.name}",
            "has_summary": (summaries_dir / f"{f.stem}_summary.json").exists(),
        })

    return render_template("docs.html", active="docs",
                           site_exists=True, base_url=base_url, doc_cards=doc_cards)


@app.route("/settings")
def settings_page():
    msg_type = request.args.get("msg_type", "")
    msg_text = request.args.get("msg", "")
    status_type, status_msg = api_key_status()
    saved_key    = load_saved_api_key()
    config        = load_config()
    provider      = config.get("llm_provider",   "claude")
    ollama_url    = config.get("ollama_url",     "http://localhost:11434")
    ollama_model  = config.get("ollama_model",   "llama3.2")
    n_results     = int(config.get("n_results",  N_RESULTS))
    system_prompt = config.get("system_prompt",  "") or DEFAULT_SYSTEM_PROMPT

    status_color = {"success": "text-success", "danger": "text-danger"}.get(
        status_type, "text-muted"
    )

    return render_template(
        "settings.html",
        active="settings",
        status_type=status_type,
        status_msg=status_msg,
        status_color=status_color,
        saved_key=saved_key,
        provider=provider,
        ollama_url=ollama_url,
        ollama_model=ollama_model,
        n_results=n_results,
        system_prompt=system_prompt,
        default_system_prompt=DEFAULT_SYSTEM_PROMPT,
        alert_type=msg_type,
        alert_text=msg_text,
    )


@app.route("/settings/save", methods=["POST"])
def settings_save():
    provider = request.form.get("llm_provider", "claude")
    updates: dict = {"llm_provider": provider}

    if provider == "claude":
        key = request.form.get("api_key", "").strip()
        if key:
            if not key.startswith("sk-ant-"):
                return redirect("/settings?msg_type=warning&msg=Invalid key (must start with sk-ant-).")
            updates["anthropic_api_key"] = key
            os.environ["ANTHROPIC_API_KEY"] = key
        msg = "Claude settings saved."
    else:
        updates["ollama_url"]   = request.form.get("ollama_url",   "http://localhost:11434").strip()
        updates["ollama_model"] = request.form.get("ollama_model", "llama3.2").strip()
        msg = f"Ollama configured: {updates['ollama_model']} ({updates['ollama_url']})."

    try:
        n_results = max(1, min(20, int(request.form.get("n_results", N_RESULTS))))
        updates["n_results"] = n_results
    except ValueError:
        pass

    updates["system_prompt"] = request.form.get("system_prompt", "").strip()

    save_config(updates)
    return redirect(f"/settings?msg_type=success&msg={msg}")


# ── API endpoints ─────────────────────────────────────────────────────────────

def _build_where(sources: list[str]) -> dict | None:
    """Return a ChromaDB where-clause for the given source list, or None for all."""
    if not sources:
        return None
    if len(sources) == 1:
        return {"source": sources[0]}
    return {"$or": [{"source": s} for s in sources]}


@app.route("/api/sources")
def api_sources():
    """Return the sorted list of unique source names indexed in ChromaDB."""
    collection = get_collection()
    all_metas  = collection.get(include=["metadatas"])["metadatas"]
    sources    = sorted({m["source"] for m in all_metas if "source" in m})
    return jsonify({"sources": sources})


@app.route("/api/filters")
def api_filters():
    """Return saved RAG filters from config."""
    filters = load_config().get("rag_filters", {})
    return jsonify({"filters": filters})


@app.route("/api/filters/save", methods=["POST"])
def api_filters_save():
    """Create or update a named filter."""
    data    = request.json or {}
    name    = (data.get("name") or "").strip()
    sources = data.get("sources") or []
    if not name:
        return jsonify({"ok": False, "error": "Name required"}), 400
    if not isinstance(sources, list) or not sources:
        return jsonify({"ok": False, "error": "At least one source required"}), 400
    filters = load_config().get("rag_filters", {})
    filters[name] = sources
    save_config({"rag_filters": filters})
    return jsonify({"ok": True})


@app.route("/api/filters/delete/<path:name>", methods=["POST"])
def api_filters_delete(name: str):
    """Delete a named filter."""
    filters = load_config().get("rag_filters", {})
    filters.pop(name, None)
    save_config({"rag_filters": filters})
    return jsonify({"ok": True})


@app.route("/api/ollama/models")
def api_ollama_models():
    """List models available in a running Ollama instance."""
    url = request.args.get("url", "http://localhost:11434").strip()
    try:
        req = urllib.request.Request(
            f"{url.rstrip('/')}/api/tags",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        models = [m["name"] for m in data.get("models", [])]
        return jsonify({"ok": True, "models": models})
    except urllib.error.URLError as e:
        return jsonify({"ok": False, "error": f"Ollama unreachable: {e.reason}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data           = request.json or {}
    message        = data.get("message", "").strip()
    history        = data.get("history", [])
    uploaded_doc   = data.get("uploaded_doc", "")
    filter_sources = data.get("filter_sources") or []

    if not message:
        return jsonify({"answer": "", "refs": "", "translations": ""})

    # Check prerequisites depending on provider
    config   = load_config()
    provider = config.get("llm_provider", "claude")
    if provider == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({
            "answer": "**Error**: no API key configured. Go to **⚙️ Settings**.",
            "refs": "", "translations": "",
        })

    try:
        question_en, question_fr = translate_question(message)
    except Exception as e:
        return jsonify({"answer": f"**Translation error**: {e}", "refs": "", "translations": ""})

    translations = (
        f"<strong>Queries sent to the index:</strong><br>"
        f"🇬🇧 {question_en}<br>🇫🇷 {question_fr}"
    )

    n_results  = int(config.get("n_results", N_RESULTS))
    where      = _build_where(filter_sources)
    collection = get_collection()
    query_kw   = {"n_results": n_results, **({"where": where} if where else {})}
    results_en = collection.query(query_texts=[question_en], **query_kw)
    results_fr = collection.query(query_texts=[question_fr], **query_kw)
    matched_ids, docs, metas = merge_results(results_en, results_fr)
    sources = [m["source"] for m in metas]

    # Expand with 2 chunks before and after each match for richer context
    neighbor_docs, neighbor_metas = _fetch_neighbor_chunks(collection, matched_ids)

    MAX_CHARS = 2000
    context_parts = [
        f"[Source: {src}]\n{doc[:MAX_CHARS]}"
        for src, doc in zip(sources, docs)
    ]
    context_parts += [
        f"[Context: {meta.get('source', '')}]\n{doc[:MAX_CHARS]}"
        for doc, meta in zip(neighbor_docs, neighbor_metas)
    ]
    context = "\n\n---\n\n".join(context_parts)

    MAX_UPLOAD = 30_000
    system_instruction = config.get("system_prompt", "").strip() or DEFAULT_SYSTEM_PROMPT
    system_parts = [system_instruction]
    if context:
        system_parts.append(f"\n## Knowledge base excerpts\n\n{context}")
    if uploaded_doc:
        system_parts.append(
            f"\n## Uploaded document (priority source)\n\n{uploaded_doc[:MAX_UPLOAD]}"
        )

    llm_messages = list(history) + [{"role": "user", "content": message}]
    system_prompt = "\n".join(system_parts)

    try:
        answer = _llm_chat(llm_messages, system=system_prompt, max_tokens=2048)
    except anthropic.AuthenticationError:
        answer = "**Error**: invalid or expired API key. Go to **⚙️ Settings**."
    except Exception as e:
        answer = f"**Error**: {e}"

    return jsonify({
        "answer":       answer,
        "refs":         build_refs_html(metas),
        "translations": translations,
    })


@app.route("/api/upload", methods=["POST"])
def api_upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"status": "", "text": ""})
    try:
        tmp_path = Path(f"/tmp/{f.filename}")
        f.save(str(tmp_path))
        converter = DocumentConverter()
        result    = converter.convert(str(tmp_path))
        text      = result.document.export_to_markdown()
        tmp_path.unlink(missing_ok=True)
        return jsonify({
            "status": f"✔ {f.filename} loaded ({len(text):,} characters)",
            "text":   text,
        })
    except Exception as e:
        return jsonify({"status": f"⚠️ Error: {e}", "text": ""})


# ── QMD sources routes ───────────────────────────────────────────────────────

@app.route("/qmd")
def qmd_list():
    quarto_dir, _ = _get_output_dirs()
    qmd_paths = sorted(
        f for f in quarto_dir.glob("*.qmd") if f.name != "index.qmd"
    )

    if not qmd_paths:
        return render_template("qmd_list.html", active="qmd", qmd_files=[])

    config    = load_config()
    site_path = _resolve_site(config.get("quarto_site", ""))
    base_url  = DOCS_BASE

    qmd_files = []
    for f in qmd_paths:
        title       = f.stem.replace("_", " ").replace("-", " ").title()
        size_kb     = round(f.stat().st_size / 1024, 1)
        html_exists = (site_path / f"{f.stem}.html").exists() if site_path.exists() else False
        qmd_files.append({
            "name":       f.name,
            "title":      title,
            "size_kb":    size_kb,
            "html_exists": html_exists,
            "html_url":   f"{base_url}/{f.stem}.html" if html_exists else "",
        })

    return render_template("qmd_list.html", active="qmd", qmd_files=qmd_files)


@app.route("/qmd/<filename>")
def qmd_view(filename: str):
    # Security: no path traversal
    if "/" in filename or "\\" in filename or not filename.endswith(".qmd"):
        return "Invalid filename", 400

    quarto_dir, _ = _get_output_dirs()
    qmd_path = quarto_dir / filename
    if not qmd_path.exists():
        return "File not found", 404

    content    = qmd_path.read_text(encoding="utf-8")
    title      = qmd_path.stem.replace("_", " ").replace("-", " ").title()
    size_kb    = round(qmd_path.stat().st_size / 1024, 1)
    line_count = content.count("\n") + 1

    # Split YAML frontmatter from body
    frontmatter, body_md = "", content
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            frontmatter = content[3:end].strip()
            body_md     = content[end + 3:].strip()

    config    = load_config()
    site_path = _resolve_site(config.get("quarto_site", ""))
    html_url  = (
        f"{DOCS_BASE}/{qmd_path.stem}.html"
        if (site_path / f"{qmd_path.stem}.html").exists()
        else None
    )

    return render_template(
        "qmd_view.html",
        active="qmd",
        title=title,
        size_kb=size_kb,
        line_count=line_count,
        frontmatter=frontmatter,
        body_md=body_md,
        content=content,
        html_url=html_url,
    )


# ── Summary pipeline ─────────────────────────────────────────────────────────

def _summaries_dir() -> Path:
    _, chunks_dir = _get_output_dirs()
    d = chunks_dir.parent / "summaries"
    d.mkdir(exist_ok=True)
    return d


def _parse_qmd_sections(qmd_text: str) -> list[tuple[str, str]]:
    """Split QMD markdown body into (heading, content) pairs.

    - Strips YAML frontmatter.
    - Splits on headings H1/H2/H3.
    - Merges consecutive sections whose body is too short into the next one.
    """
    # Remove frontmatter
    body = qmd_text
    if body.lstrip().startswith("---"):
        end = body.find("---", 3)
        if end != -1:
            body = body[end + 3:].strip()

    sections: list[tuple[str, str]] = []
    current_heading = "(Introduction)"
    current_lines: list[str] = []

    for line in body.split("\n"):
        m = re.match(r'^(#{1,3})\s+(.+)', line)
        if m:
            content = "\n".join(current_lines).strip()
            if content:
                sections.append((current_heading, content))
            current_heading = m.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)

    content = "\n".join(current_lines).strip()
    if content:
        sections.append((current_heading, content))

    # Merge sections with < 120 chars of content into the following one
    merged: list[tuple[str, str]] = []
    carry_heading, carry_body = "", ""
    for i, (heading, text) in enumerate(sections):
        if carry_heading:
            heading = f"{carry_heading} / {heading}"
            text    = carry_body + "\n\n" + text
            carry_heading, carry_body = "", ""
        if len(text) < 120 and i < len(sections) - 1:
            carry_heading, carry_body = heading, text
        else:
            merged.append((heading, text))
    if carry_heading:
        merged.append((carry_heading, carry_body))

    return merged


def _generate_summary_task(stem: str, job_id: str) -> None:
    """Generate section-by-section summary from the QMD file, save to JSON."""
    import datetime

    q = _jobs[job_id]

    def put(**kwargs) -> None:
        q.put(kwargs)

    try:
        cfg      = load_config()
        provider = cfg.get("llm_provider", "claude")
        if provider == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
            put(type="error", msg="❌ No API key — go to Settings.", done=True)
            return

        quarto_dir, _ = _get_output_dirs()
        qmd_path = quarto_dir / f"{stem}.qmd"
        if not qmd_path.exists():
            put(type="error", msg=f"❌ QMD file not found: <em>{stem}.qmd</em>.", done=True)
            return

        qmd_text = qmd_path.read_text(encoding="utf-8")
        title    = stem.replace("_", " ").replace("-", " ").title()

        model_label = (
            f"🦙 Ollama ({cfg.get('ollama_model', 'llama3.2')})"
            if provider == "ollama" else f"🤖 Claude ({CLAUDE_MODEL})"
        )
        put(type="log", msg=f"📄 Reading <strong>{stem}.qmd</strong> "
                            f"({len(qmd_text):,} chars) — {model_label}")

        # ── Parse sections from QMD ───────────────────────────────────────────
        section_items = _parse_qmd_sections(qmd_text)
        MAX_SECTIONS  = 20
        section_items = section_items[:MAX_SECTIONS]
        put(type="log", msg=f"  → {len(section_items)} section(s) identified")

        # ── Summarize each section ────────────────────────────────────────────
        section_summaries: list[dict] = []
        for i, (heading, text) in enumerate(section_items, 1):
            excerpt = text[:4000]
            put(type="log",
                msg=f"🔄 [{i}/{len(section_items)}] <em>{heading}</em>…")
            try:
                summary = _llm_chat(
                    messages=[{"role": "user", "content": (
                        f"Summarize the following section titled \"{heading}\" "
                        f"in 2-4 sentences. Be concise and informative. "
                        f"Reply in the same language as the text.\n\n{excerpt}"
                    )}],
                    max_tokens=350,
                ).strip()
            except Exception as e:
                summary = f"(error: {e})"

            section_summaries.append({"heading": heading, "summary": summary})
            put(type="section", heading=heading, summary=summary,
                msg=f"✔ <strong>{heading}</strong>")

        # ── Global summary ────────────────────────────────────────────────────
        put(type="log", msg="🔄 Generating executive summary…")
        bullets = "\n".join(
            f"- {s['heading']}: {s['summary']}" for s in section_summaries
        )
        try:
            global_summary = _llm_chat(
                messages=[{"role": "user", "content": (
                    f"Based on these section summaries from \"{title}\", "
                    f"write a 3-5 sentence executive summary.\n\n{bullets}"
                )}],
                max_tokens=450,
            ).strip()
        except Exception as e:
            global_summary = f"(error: {e})"

        # ── Save ──────────────────────────────────────────────────────────────
        data = {
            "title":          title,
            "stem":           stem,
            "generated_at":   datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "global_summary": global_summary,
            "sections":       section_summaries,
        }
        save_path = _summaries_dir() / f"{stem}_summary.json"
        save_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        put(type="done", msg="✅ Summary generated and saved.", data=data)

    except Exception as exc:
        q.put({"type": "error", "msg": f"❌ Unexpected error: {exc}", "done": True})


# ── Summary routes ────────────────────────────────────────────────────────────

@app.route("/summary/<stem>")
def summary_page(stem: str):
    if "/" in stem or "\\" in stem:
        return "Invalid", 400

    regen     = request.args.get("regen") == "1"
    save_path = _summaries_dir() / f"{stem}_summary.json"

    # Saved summary exists and no forced regeneration → display it
    if save_path.exists() and not regen:
        data = json.loads(save_path.read_text(encoding="utf-8"))
        config    = load_config()
        site_path = _resolve_site(config.get("quarto_site", ""))
        html_url  = (
            f"{DOCS_BASE}/{stem}.html"
            if (site_path / f"{stem}.html").exists() else None
        )
        return render_template("summary.html", active="docs",
                               saved_summary=True, data=data, stem=stem,
                               html_url=html_url, title=data["title"])

    # Need to generate
    title = stem.replace("_", " ").replace("-", " ").title()
    return render_template("summary.html", active="docs",
                           saved_summary=False, data=None, stem=stem,
                           html_url=None, title=title)


@app.route("/api/summary/stream/<stem>")
def api_summary_stream(stem: str):
    if "/" in stem or "\\" in stem:
        return "Invalid", 400

    job_id        = uuid.uuid4().hex[:10]
    _jobs[job_id] = queue.Queue()

    threading.Thread(
        target=_generate_summary_task,
        args=(stem, job_id),
        daemon=True,
    ).start()

    def generate():
        while True:
            item = _jobs[job_id].get()
            yield f"data: {json.dumps(item)}\n\n"
            if item.get("type") in ("done", "error"):
                break
        _jobs.pop(job_id, None)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Document delete route ────────────────────────────────────────────────────

@app.route("/api/doc/delete/<stem>", methods=["POST"])
def api_doc_delete(stem: str):
    """Remove a document entirely: ChromaDB + chunks + QMD + HTML + summary."""
    if "/" in stem or "\\" in stem or ".." in stem:
        return jsonify({"error": "Invalid stem"}), 400

    quarto_dir, chunks_dir = _get_output_dirs()
    config        = load_config()
    site_path     = _resolve_site(config.get("quarto_site", ""))
    summaries_dir = chunks_dir.parent / "summaries"

    removed: list[str] = []
    errors:  list[str] = []

    # 1. ChromaDB
    try:
        collection  = get_collection()
        source_name = f"{stem}_chunks"
        existing    = collection.get(where={"source": source_name})
        n = len(existing["ids"])
        if n:
            collection.delete(ids=existing["ids"])
        removed.append(f"{n} chunk(s) deleted from ChromaDB")
    except Exception as e:
        errors.append(f"ChromaDB: {e}")

    # 2. Chunks JSON
    chunks_json = chunks_dir / f"{stem}_chunks.json"
    if chunks_json.exists():
        chunks_json.unlink()
        removed.append(chunks_json.name)

    # 3. QMD + images
    qmd_file = quarto_dir / f"{stem}.qmd"
    if qmd_file.exists():
        qmd_file.unlink()
        removed.append(qmd_file.name)
    images_dir = quarto_dir / f"{stem}_files"
    if images_dir.exists():
        shutil.rmtree(images_dir)
        removed.append(f"{images_dir.name}/")

    # 4. HTML + HTML support files in _site
    if site_path.exists():
        html_file = site_path / f"{stem}.html"
        if html_file.exists():
            html_file.unlink()
            removed.append(f"_site/{html_file.name}")
        html_assets = site_path / f"{stem}_files"
        if html_assets.exists():
            shutil.rmtree(html_assets)
            removed.append(f"_site/{html_assets.name}/")

    # 5. Summary
    summary_file = summaries_dir / f"{stem}_summary.json"
    if summary_file.exists():
        summary_file.unlink()
        removed.append(summary_file.name)

    # 6. Rebuild Quarto index files
    try:
        _rebuild_quarto_index(quarto_dir, chunks_dir)
        removed.append("index.qmd regenerated")
    except Exception as e:
        errors.append(f"index rebuild: {e}")

    # 7. Quarto render in background (updates _site/index.html)
    def _bg_render() -> None:
        try:
            subprocess.run(
                ["quarto", "render"],
                cwd=str(quarto_dir),
                capture_output=True, text=True, timeout=300,
            )
        except Exception:
            pass

    threading.Thread(target=_bg_render, daemon=True).start()

    return jsonify({"ok": not errors, "removed": removed, "errors": errors})


# ── Ingest routes ────────────────────────────────────────────────────────────

@app.route("/ingest")
def ingest_page():
    exts   = ", ".join(sorted(SUPPORTED_EXTENSIONS))
    accept = ",".join(sorted(SUPPORTED_EXTENSIONS))
    return render_template("ingest.html", active="ingest", exts=exts, accept=accept)


@app.route("/api/ingest/upload", methods=["POST"])
def api_ingest_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file received"}), 400

    suffix = Path(f.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        return jsonify({"error": f"Unsupported format: {suffix}"}), 400

    # Save to doc_input/
    doc_input = APP_DIR / "doc_input"
    doc_input.mkdir(exist_ok=True)
    dest = doc_input / f.filename
    f.save(str(dest))

    job_id       = uuid.uuid4().hex[:10]
    _jobs[job_id] = queue.Queue()

    threading.Thread(
        target=_process_file_task,
        args=(dest, job_id),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/api/ingest/stream/<job_id>")
def api_ingest_stream(job_id: str):
    q = _jobs.get(job_id)
    if q is None:
        return jsonify({"error": "Unknown job"}), 404

    def generate():
        while True:
            item = q.get()
            yield f"data: {json.dumps(item)}\n\n"
            if item.get("done"):
                break
        _jobs.pop(job_id, None)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    saved_key = load_saved_api_key()
    if saved_key:
        os.environ["ANTHROPIC_API_KEY"] = saved_key
        print("API key loaded from config.")

    site_path = _get_site_path()
    if site_path.exists():
        start_docs_server(site_path, DOCS_PORT)
    else:
        print(f"Warning: Quarto site not found at {site_path} — will start after first ingest.")

    print(f"Starting Flask app at {APP_BASE}/")
    app.run(host="0.0.0.0", port=APP_PORT, debug=False)
