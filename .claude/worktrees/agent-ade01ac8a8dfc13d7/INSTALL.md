# Installation — Drag and Rag

Guide d'installation complet pour Linux et macOS.

---

## Prérequis

| Outil | Version minimale | Vérification |
|---|---|---|
| Python | 3.10+ | `python3 --version` |
| pip | inclus avec Python | `pip3 --version` |
| Quarto | 1.4+ | `quarto --version` |
| Clé API Anthropic | — | [console.anthropic.com](https://console.anthropic.com/settings/keys) |

---

## 1. Installer Quarto

Quarto est le moteur de rendu de la documentation HTML. Il doit être installé séparément de Python.

**Linux (Debian/Ubuntu) :**
```bash
# Télécharger le .deb depuis https://quarto.org/docs/get-started/
wget https://github.com/quarto-dev/quarto-cli/releases/download/v1.9.37/quarto-1.9.37-linux-amd64.deb
sudo dpkg -i quarto-1.9.37-linux-amd64.deb
quarto --version   # vérification
```

**macOS :**
```bash
brew install quarto
```

---

## 2. Cloner ou copier le projet

```bash
# Si vous avez git :
git clone <url-du-repo> drag-and-rag
cd drag-and-rag

# Sinon, copier les fichiers manuellement dans un dossier :
mkdir drag-and-rag && cd drag-and-rag
# → copier app_flask.py, process_documents.py, embed_and_query.py,
#          requirements.txt dans ce dossier
```

---

## 3. Créer l'environnement virtuel Python

```bash
# Créer l'environnement (une seule fois)
python3 -m venv env

# Activer l'environnement
source env/bin/activate          # Linux / macOS
# env\Scripts\activate           # Windows

# Vérification : le prompt doit afficher (env)
```

> ⚠️ **À faire à chaque nouvelle session** : `source env/bin/activate`

---

## 4. Installer les dépendances Python

```bash
# Vérifier que l'environnement est actif (prompt affiche (env))
pip install -r requirements.txt
```

L'installation prend quelques minutes — `docling` et `sentence-transformers`
téléchargent des modèles lors du premier usage.

| Package | Rôle |
|---|---|
| `flask` | Interface web |
| `anthropic` | API Claude (traduction + réponses RAG) |
| `docling` | Conversion de documents (PDF, DOCX, PPTX…) |
| `chromadb` | Base vectorielle locale |
| `sentence-transformers` | Modèle d'embedding local (`all-MiniLM-L6-v2`) |

---

## 5. Obtenir une clé API Anthropic

1. Créer un compte sur [console.anthropic.com](https://console.anthropic.com)
2. Aller dans **Settings → API Keys → Create Key**
3. Copier la clé (format `sk-ant-api03-…`)

La clé sera saisie directement dans l'interface web (onglet ⚙️ Settings) et
sera sauvegardée dans `.rag_config.json` localement.

---

## 6. Lancer l'application

```bash
# S'assurer que l'environnement est actif
source env/bin/activate

# Démarrer le serveur Flask
python3 app_flask.py
```

Ouvrir dans le navigateur : **http://localhost:7860**

L'application démarre avec 5 onglets :
- 💬 **Chat** — poser des questions sur les documents
- 📚 **Documentation** — parcourir les docs HTML générés
- 📝 **Sources** — voir les fichiers `.qmd` bruts
- 📤 **Ingest** — ajouter de nouveaux documents par glisser-déposer
- ⚙️ **Settings** — configurer la clé API

---

## 7. Ajouter vos premiers documents

### Via l'interface web (recommandé)

1. Aller dans l'onglet **📤 Ingest**
2. Glisser-déposer un fichier PDF, DOCX, PPTX, HTML, Markdown ou XLSX
3. Attendre la fin du pipeline (conversion → chunks → ChromaDB → rendu Quarto)
4. Le document est immédiatement disponible dans Chat et Documentation

### Via la ligne de commande

```bash
# Placer les documents dans doc_input/
python3 process_documents.py doc_input/ doc_output/

# Indexer les chunks dans ChromaDB
python3 embed_and_query.py index doc_output/chunks/

# Rendre la documentation Quarto
cd doc_output/quarto && quarto render && cd ../..

# Configurer le chemin du site dans .rag_config.json
# (fait automatiquement par l'interface web)
```

---

## Structure des fichiers générés

```
drag-and-rag/
├── app_flask.py            # Application web Flask
├── process_documents.py    # Pipeline de conversion
├── embed_and_query.py      # Indexation ChromaDB
├── requirements.txt        # Dépendances Python
│
├── doc_input/              # ← Déposer vos documents ici
├── doc_output/
│   ├── chunks/             # Fragments JSON (texte + métadonnées)
│   ├── summaries/          # Résumés IA générés (cache)
│   └── quarto/
│       ├── *.qmd           # Pages Quarto générées
│       ├── _quarto.yml     # Configuration du site
│       └── _site/          # ← Site HTML rendu (servi sur port 8080)
│
├── .chroma_db/             # Base vectorielle ChromaDB (auto-créé)
├── .rag_config.json        # Configuration locale (clé API, chemins)
└── env/                    # Environnement virtuel Python
```

---

## Formats de documents supportés

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

## Dépannage

### `ModuleNotFoundError: No module named 'flask'`
→ L'environnement virtuel n'est pas activé.
```bash
source env/bin/activate
```

### `Error: no API key set`
→ Aller dans l'onglet ⚙️ Settings et saisir la clé API.

### La documentation ne s'affiche pas
→ Vérifier que `quarto render` a bien été lancé (automatique via Ingest, sinon manuellement) :
```bash
cd doc_output/quarto && quarto render
```
→ Vérifier que le chemin du site est configuré dans `.rag_config.json` :
```json
{
  "quarto_site": "/chemin/absolu/vers/doc_output/quarto/_site"
}
```

### Erreur `address already in use` au démarrage
→ Un autre processus utilise le port 7860 ou 8080. Tuer le processus :
```bash
# Trouver et tuer le processus sur le port 7860
lsof -ti:7860 | xargs kill -9
```

### Premier lancement lent
→ Normal : `sentence-transformers` télécharge le modèle `all-MiniLM-L6-v2`
(~90 Mo) lors du premier appel. Les lancements suivants sont instantanés.

---

## Installation système (démarrage automatique)

Pour que l'application se lance automatiquement au démarrage de l'ordinateur,
utilisez le script d'installation système qui déploie l'app dans `/opt/drag_and_rag`
et configure un service **systemd**.

### Installation

```bash
sudo ./install.sh
```

Le script :
1. Copie le projet dans `/opt/drag_and_rag` (avec rsync)
2. Crée un environnement virtuel dédié `/opt/drag_and_rag/env`
3. Installe les dépendances Python
4. Met à jour les chemins dans `.rag_config.json`
5. Crée et active le service systemd `drag_and_rag`

L'application est ensuite disponible sur **http://localhost:7860** à chaque démarrage.

### Commandes de gestion du service

```bash
sudo systemctl status  drag_and_rag     # état du service
sudo systemctl restart drag_and_rag     # relancer
sudo systemctl stop    drag_and_rag     # arrêter
sudo systemctl disable drag_and_rag     # ne plus lancer au démarrage
sudo journalctl -u drag_and_rag -f      # logs en direct
```

### Mise à jour après modification du code

```bash
# Depuis le répertoire source (pas /opt)
sudo ./install.sh    # idempotent — met à jour sans perdre les données
```

### Désinstallation système

```bash
sudo ./uninstall.sh
```

Arrête le service, le retire de systemd et supprime `/opt/drag_and_rag`.

---

## Mise à jour (mode développement)

```bash
source env/bin/activate
pip install -r requirements.txt --upgrade
```

---

## Désinstallation (mode développement)

```bash
# Supprimer l'environnement virtuel et les données générées
rm -rf env/ .chroma_db/ doc_output/ .rag_config.json
```
