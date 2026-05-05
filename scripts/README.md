# Local ingestion

This script mirrors the Fabric ingestion notebooks locally using `DefaultAzureCredential`.

## Setup

Ensure the repo root `.env` file contains these values:

- `AZURE_SEARCH_ENDPOINT`
- `AZURE_AI_FOUNDRY_ENDPOINT`
- `AZURE_DOC_INTELLIGENCE_ENDPOINT`
- `AZURE_AI_FOUNDRY_CHAT_DEPLOYMENT` (default: `model-router`)
- `AZURE_AI_FOUNDRY_EMBEDDING_DEPLOYMENT` (default: `text-embedding-3-large`)

Install dependencies and run (`azure-ai-inference` currently installs from the published beta package range):

```powershell
cd scripts
python -m pip install -r requirements.txt
python local_ingest.py
```

Useful options:

```powershell
python local_ingest.py --max-pdfs 2 --skip-existing
python local_ingest.py --data-dir ..\data --no-summary
```

The script:
- creates the `nav-chunks` and `nav-documents` Azure AI Search indexes,
- parses PDFs from `data\`,
- chunks by section with token-aware overlap,
- generates summaries and embeddings,
- uploads documents to Azure AI Search.
