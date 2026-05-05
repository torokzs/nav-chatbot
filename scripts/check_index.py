from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
cred = DefaultAzureCredential()

# Check documents index
print("=== DOCUMENTS INDEX ===")
client = SearchClient("https://srch-whnqec-6otgod.search.windows.net", "nav-documents", cred)
results = client.search(search_text="tao társasági adó túlfizetés felajánlás", top=5)
for r in results:
    print(f"  {r.get('fuzet_szam','?')} | {r.get('title','?')[:80]}")

# Check chunks index
print("\n=== CHUNKS INDEX (tao túlfizetés) ===")
chunk_client = SearchClient("https://srch-whnqec-6otgod.search.windows.net", "nav-chunks", cred)
results = chunk_client.search(search_text="tao túlfizetés teendő", top=5)
for r in results:
    print(f"  {r.get('fuzet_szam','?')} p{r.get('page_from','?')} | {r.get('breadcrumb','')[:60]}")
    print(f"    {r.get('content','')[:120]}")

# Count total docs/chunks
print(f"\nTotal documents: {client.get_document_count()}")
print(f"Total chunks: {chunk_client.get_document_count()}")
