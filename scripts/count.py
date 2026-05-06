import os
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
import logging
logging.getLogger("azure").setLevel(logging.WARNING)
SEARCH_ENDPOINT = os.environ["AI_SEARCH_ENDPOINT"]
cred = DefaultAzureCredential()
chunk_client = SearchClient(SEARCH_ENDPOINT, "nav-chunks", cred)
doc_client = SearchClient(SEARCH_ENDPOINT, "nav-documents", cred)
print(f"Total chunks: {chunk_client.get_document_count()}")
print(f"Total documents: {doc_client.get_document_count()}")
