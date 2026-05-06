import os
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
SEARCH_ENDPOINT = os.environ["AI_SEARCH_ENDPOINT"]
cred = DefaultAzureCredential()
client = SearchClient(SEARCH_ENDPOINT, "nav-documents", cred)
results = client.search(search_text="*", top=1)
for r in results:
    print(list(r.keys()))
    for k,v in r.items():
        if k.startswith("@") or k.endswith("_vector"):
            continue
        print(f"  {k}: {str(v)[:100]}")
