# Installation — Dragon

Complete installation guide for Linux and macOS.

---

## Prerequisites

| Tool | Minimum version | Check |
|---|---|---|
| Python | 3.10+ | `python3 --version` |
| pip | bundled with Python | `pip3 --version` |
| Quarto | 1.4+ | `quarto --version` |
| LLM API key | — | see [§ 5 — API keys](#5-configure-an-llm-provider) |

---

## 1. Install Quarto

Quarto renders the documentation as a static HTML site. It must be installed separately from Python.

**Linux (Debian/Ubuntu):**
```bash
# Download the .deb from https://quarto.org/docs/get-started/
wget https://github.com/quarto-dev/quarto-cli/releases/download/v1.9.37/quarto-1.9.37-linux-amd64.deb
sudo dpkg -i quarto-1.9.37-linux-amd64.deb
quarto --version   # verify
```

**macOS:**
```bash
brew install quarto
```

---

## 2. Clone or copy the project

```bash
# With git:
git clone <repo-url> dragon
cd dragon

# Without git — copy the files manually:
mkdir dragon && cd dragon
# → copy app_flask.py, process_documents.py, ingest_dir.py,
#          requirements.txt, static/, templates/ into this folder
```

---

## 3. Create a Python virtual environment

```bash
# Create the environment (once only)
python3 -m venv env

# Activate it
source env/bin/activate          # Linux / macOS
# env\Scripts\activate           # Windows

# Your prompt should now show (env)
```

> ⚠️ **Activate the environment at every new session:** `source env/bin/activate`

---

## 4. Install Python dependencies

```bash
# Make sure the environment is active (prompt shows (env))
pip install -r requirements.txt
```

Installation takes a few minutes — `docling` and `sentence-transformers` download
their models on first use.

| Package | Role |
|---|---|
| `flask` | Web interface |
| `anthropic` | Claude API (translation + RAG answers) |
| `docling` | Document conversion (PDF, DOCX, PPTX…) |
| `chromadb` | Local vector database |
| `sentence-transformers` | Local embedding model (`all-MiniLM-L6-v2`) |

### Optional packages (for Gemini or OpenAI)

If you plan to use Google Gemini or OpenAI/ChatGPT as your LLM provider, install the
corresponding package:

```bash
pip install google-generativeai   # Google Gemini
pip install openai                 # OpenAI / ChatGPT
```

These packages are imported lazily — the application starts normally even if they are
not installed. A clear error is shown in the chat if a missing package is needed.

---

## 5. Configure an LLM provider

Dragon supports four LLM providers. Configure the one you want in the **⚙️ Settings** tab
after starting the app — the key is stored locally in `.rag_config.json`.

| Provider | Where to get a key |
|---|---|
| **Claude** (Anthropic) | [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) |
| **Gemini** (Google) | [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) |
| **ChatGPT** (OpenAI) | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| **Ollama** (local) | No key needed — install [ollama.com](https://ollama.com/download), then `ollama pull <model>` |

---

## 6. Start the application

```bash
# Make sure the environment is active
source env/bin/activate

# Start the Flask server
python3 app_flask.py
```

Open in your browser: **http://localhost:7860**

The application has six tabs:

| Tab | Description |
|---|---|
| 💬 **Chat** | Ask questions about your documents with real-time reasoning display |
| 📚 **Documentation** | Browse the rendered HTML documentation site |
| 📝 **Sources** | Inspect the raw `.qmd` source files |
| 📤 **Ingest** | Add new documents via drag-and-drop |
| ⚙️ **Settings** | Configure your LLM provider, RAG parameters, and system prompt |
| 🐉 **About** | App information |

> The app is also accessible from other devices on your local network at the IP address
> printed in the terminal at startup.

---

## 7. Add your first documents

### Via the web interface (recommended)

1. Go to the **📤 Ingest** tab
2. Drag and drop a PDF, DOCX, PPTX, HTML, Markdown, or XLSX file
3. Watch the real-time pipeline: conversion → chunks → ChromaDB → Quarto render
4. The document is immediately available in Chat and Documentation

### Via the command line (batch ingestion)

Use `ingest_dir.py` to ingest an entire directory at once:

```bash
python3 ingest_dir.py /path/to/documents/

# Options:
#   --output   custom output directory (default: doc_output/)
#   --db       custom ChromaDB directory (default: .chroma_db/)
#   --no-render  skip the Quarto render step
#   --force    re-ingest documents that are already indexed
```

---

## File structure

```
dragon/
├── app_flask.py            # Flask web application
├── process_documents.py    # Document conversion pipeline
├── ingest_dir.py           # CLI batch ingestion tool
├── requirements.txt        # Python dependencies
│
├── static/
│   ├── css/dragon.css      # Application stylesheet
│   └── *.svg / *.json      # Icons, manifest, service worker
├── templates/              # Jinja2 HTML templates
│   ├── base.html
│   ├── chat.html
│   ├── docs.html
│   └── …
│
├── doc_input/              # ← Drop your source documents here
├── doc_output/
│   ├── chunks/             # JSON fragment files (text + metadata)
│   ├── summaries/          # AI-generated summaries (cache)
│   └── quarto/
│       ├── *.qmd           # Generated Quarto pages
│       ├── _quarto.yml     # Site configuration
│       └── _site/          # ← Rendered HTML site (served on port 8080)
│
├── .chroma_db/             # ChromaDB vector database (auto-created)
├── .rag_config.json        # Local configuration (API keys, paths, settings)
└── env/                    # Python virtual environment
```

---

## Supported document formats

| Format | Extension |
|---|---|
| PDF | `.pdf` |
| Word | `.docx` |
| PowerPoint | `.pptx` |
| HTML | `.html` |
| Markdown | `.md` |
| AsciiDoc | `.adoc` |
| Excel | `.xlsx` |

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'flask'`
The virtual environment is not activated.
```bash
source env/bin/activate
```

### `Error: no API key configured`
Go to the **⚙️ Settings** tab and enter your API key for the selected provider.

### Documentation tab is empty
Make sure `quarto render` has been run (happens automatically after an Ingest, or
manually):
```bash
cd doc_output/quarto && quarto render
```
Also verify that the site path is set in `.rag_config.json`:
```json
{
  "quarto_site": "/absolute/path/to/doc_output/quarto/_site"
}
```

### `address already in use` at startup
Another process is using port 7860 or 8080. Kill it:
```bash
lsof -ti:7860 | xargs kill -9
lsof -ti:8080 | xargs kill -9
```

### Slow first startup
Expected behaviour: `sentence-transformers` downloads the `all-MiniLM-L6-v2` model
(~90 MB) on first use. Subsequent startups are instant.

### Gemini or OpenAI not working
Make sure the optional package is installed:
```bash
pip install google-generativeai   # for Gemini
pip install openai                 # for OpenAI
```

---

## System-wide installation (auto-start)

To have Dragon start automatically at system boot, use the installation script which
deploys the app to `/opt/drag_and_rag` and configures a **systemd** service.

### Install

```bash
sudo ./install.sh
```

The script:
1. Copies the project to `/opt/drag_and_rag` (via rsync)
2. Creates a dedicated virtual environment `/opt/drag_and_rag/env`
3. Installs Python dependencies
4. Updates paths in `.rag_config.json`
5. Creates and enables the `drag_and_rag` systemd service

The app is then available at **http://localhost:7860** on every boot.

### Service management

```bash
sudo systemctl status  drag_and_rag     # service status
sudo systemctl restart drag_and_rag     # restart
sudo systemctl stop    drag_and_rag     # stop
sudo systemctl disable drag_and_rag     # disable auto-start
sudo journalctl -u drag_and_rag -f      # live logs
```

### Update after code changes

```bash
# From the source directory (not /opt)
sudo ./install.sh    # idempotent — updates without losing data
```

### Uninstall

```bash
sudo ./uninstall.sh
```

Stops the service, removes it from systemd, and deletes `/opt/drag_and_rag`.

---

## Update (development mode)

```bash
source env/bin/activate
pip install -r requirements.txt --upgrade
```

---

## Uninstall (development mode)

```bash
# Remove the virtual environment and generated data
rm -rf env/ .chroma_db/ doc_output/ .rag_config.json
```
