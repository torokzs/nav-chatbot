# METADATA ----------
# title: 01 - Ingest NAV PDFs to Lakehouse
# description: Uploads NAV 2026 information booklet PDFs to Lakehouse Files/raw/2026/

# COMMAND ----------

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

try:
    from notebookutils import fs as notebook_fs  # type: ignore
except ImportError:
    try:
        from mssparkutils import fs as notebook_fs  # type: ignore
    except ImportError as exc:  # pragma: no cover - notebook runtime dependency
        raise RuntimeError("This notebook must run inside Microsoft Fabric or Synapse.") from exc


SOURCE_PATH = os.getenv("SOURCE_PATH", "")
TARGET_RELATIVE_PATH = os.getenv("TARGET_RELATIVE_PATH", "raw/2026")
LOCAL_DATA_FALLBACK = os.getenv(
    "LOCAL_DATA_FALLBACK",
    r"C:\Users\jator\source\repos\navchatbotprod\data",
)


# COMMAND ----------


def files_path(relative_path: str = "") -> str:
    relative_path = relative_path.strip("/")
    return f"Files/{relative_path}" if relative_path else "Files"


TARGET_PATH = files_path(TARGET_RELATIVE_PATH)


def ensure_folder(path: str) -> None:
    try:
        notebook_fs.mkdirs(path)
    except Exception:
        pass



def list_existing_file_names(path: str) -> set[str]:
    try:
        return {entry.name for entry in notebook_fs.ls(path) if entry.name.lower().endswith(".pdf")}
    except Exception:
        return set()



def is_lakehouse_source(path: str) -> bool:
    lowered = path.lower()
    return lowered.startswith("files/") or lowered.startswith("abfss://") or lowered.startswith("onelake://")



def list_local_pdf_files(path: str) -> list[Path]:
    base = Path(path)
    if not base.exists():
        return []
    return sorted([item for item in base.iterdir() if item.is_file() and item.suffix.lower() == ".pdf"])



def list_lakehouse_pdf_files(path: str) -> list[object]:
    return [entry for entry in notebook_fs.ls(path) if entry.name.lower().endswith(".pdf")]


# COMMAND ----------


def copy_local_pdfs(source_dir: str, target_path: str, existing_names: set[str]) -> tuple[int, int]:
    uploaded = 0
    skipped = 0

    for pdf_path in list_local_pdf_files(source_dir):
        target_file = f"{target_path.rstrip('/')}/{pdf_path.name}"
        if pdf_path.name in existing_names:
            print(f"Skipping existing file: {pdf_path.name}")
            skipped += 1
            continue

        notebook_fs.cp(f"file:{pdf_path}", target_file)
        existing_names.add(pdf_path.name)
        uploaded += 1
        print(f"Uploaded {pdf_path.name} -> {target_file}")

    return uploaded, skipped



def copy_lakehouse_pdfs(source_dir: str, target_path: str, existing_names: set[str]) -> tuple[int, int]:
    uploaded = 0
    skipped = 0

    for entry in list_lakehouse_pdf_files(source_dir):
        source_file = entry.path
        target_file = f"{target_path.rstrip('/')}/{entry.name}"
        if entry.name in existing_names:
            print(f"Skipping existing file: {entry.name}")
            skipped += 1
            continue

        notebook_fs.cp(source_file, target_file)
        existing_names.add(entry.name)
        uploaded += 1
        print(f"Copied {entry.name} -> {target_file}")

    return uploaded, skipped



def resolve_source_path() -> str:
    candidates: Iterable[str] = (
        SOURCE_PATH,
        files_path("incoming/2026"),
        files_path("source/2026"),
        LOCAL_DATA_FALLBACK,
    )

    for candidate in candidates:
        if not candidate:
            continue
        if is_lakehouse_source(candidate):
            try:
                notebook_fs.ls(candidate)
                print(f"Using Lakehouse source path: {candidate}")
                return candidate
            except Exception:
                continue
        if Path(candidate).exists():
            print(f"Using local source path: {candidate}")
            return candidate

    raise FileNotFoundError(
        "No PDF source path was found. Set SOURCE_PATH to a local or Lakehouse folder before running the notebook."
    )


# COMMAND ----------

ensure_folder(TARGET_PATH)
existing_names = list_existing_file_names(TARGET_PATH)
resolved_source = resolve_source_path()

if is_lakehouse_source(resolved_source):
    uploaded_count, skipped_count = copy_lakehouse_pdfs(resolved_source, TARGET_PATH, existing_names)
else:
    uploaded_count, skipped_count = copy_local_pdfs(resolved_source, TARGET_PATH, existing_names)

current_total = len(list_existing_file_names(TARGET_PATH))
print("Ingestion complete.")
print(f"Uploaded PDFs: {uploaded_count}")
print(f"Skipped PDFs: {skipped_count}")
print(f"Current total PDFs in {TARGET_PATH}: {current_total}")


# COMMAND ----------

print("Alternative upload options:")
print("1. Manual: open the Lakehouse in Fabric and upload the PDFs into Files/raw/2026/.")
print("2. REST/API example: PUT the file to the Lakehouse Files endpoint or OneLake DFS path, then rerun this notebook.")
print("   Example target pattern: https://onelake.dfs.fabric.microsoft.com/<workspace>/<lakehouse>/Files/raw/2026/<file>.pdf")
print("3. Attached source: mount or attach the source folder, then set SOURCE_PATH to that folder and rerun the notebook.")
