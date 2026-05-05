# Fabric notebooks for the NAV RAG pipeline

This folder contains Microsoft Fabric notebook sources for the NAV PDF ingestion, parsing, embedding, indexing, and evaluation pipeline.

## Prerequisites

- A Fabric workspace with a default Lakehouse attached to the notebook session
- Workspace or user access that allows:
  - Reading and writing Lakehouse Files and Tables
  - Calling Azure AI Document Intelligence
  - Calling Azure AI Foundry chat + embedding deployments
  - Managing Azure AI Search indexes and documents
- Python packages available in the Fabric runtime or installed as workspace libraries:
  - `azure-identity`
  - `azure-ai-documentintelligence`
  - `azure-ai-inference`
  - `azure-search-documents`
  - `tiktoken`

## Required configuration

Set these values either as notebook parameters, environment variables, or Spark configuration values.

### Parsing notebook

- `DOCUMENT_INTELLIGENCE_ENDPOINT`

### Embedding and indexing notebooks

- `AI_FOUNDRY_ENDPOINT`
- `AI_FOUNDRY_CHAT_MODEL` (model-router deployment for summarization and judging)
- `AI_FOUNDRY_EMBED_MODEL` (`text-embedding-3-large` deployment)
- `AI_SEARCH_ENDPOINT`
- Optional: `AI_SEARCH_CHUNKS_INDEX` (default: `nav-chunks`)
- Optional: `AI_SEARCH_DOCUMENTS_INDEX` (default: `nav-documents`)
- Optional: `AI_SEARCH_SYNONYM_MAP` (default: `nav-tax-synonyms`)

### Optional overrides

- `SOURCE_PATH` for `01_ingest_pdfs.py`
- `RAW_RELATIVE_PATH` for `02_parse_chunk.py`
- `EVAL_DATASET_PATH` for `04_eval_notebook.py`

## Notebook order

1. `01_ingest_pdfs.py`
   - Copies PDF files into `Files/raw/2026/`
   - Skips files that already exist
2. `02_parse_chunk.py`
   - Parses PDFs with Azure AI Document Intelligence
   - Builds section-aware chunks and writes `nav_chunks` and `nav_documents`
3. `03_embed_index.py`
   - Generates Hungarian summaries
   - Creates embeddings
   - Creates or updates Azure AI Search indexes and uploads documents
4. `04_eval_notebook.py`
   - Loads an evaluation JSONL dataset
   - Runs retrieval and answer generation
   - Writes per-question metrics to `eval_results`

## Running in Fabric

1. Open the notebook in Fabric.
2. Attach the correct default Lakehouse.
3. Set parameters or Spark config values for the Azure endpoints and model names.
4. Run the notebooks in sequence.

## Expected Lakehouse outputs

- `Files/raw/2026/` – raw NAV PDF files
- `Tables/nav_chunks` – chunk-level dataset
- `Tables/nav_documents` – booklet-level dataset
- `Tables/eval_results` – evaluation runs and metrics

## Evaluation dataset format

The evaluation notebook expects JSONL records with at least these fields:

```json
{"question": "Mikor kell adószámot kérni?", "expected_answer": "...", "expected_fuzet": "01", "expected_page": 4}
```

The notebook tries a supplied path first and then a few common repository and Lakehouse locations.
