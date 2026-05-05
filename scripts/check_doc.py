from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
cred = DefaultAzureCredential()
client = SearchClient("https://srch-whnqec-6otgod.search.windows.net", "nav-documents", cred)
results = client.search(search_text="*", top=1)
for r in results:
    print(list(r.keys()))
    for k,v in r.items():
        if k.startswith("@") or k.endswith("_vector"):
            continue
        print(f"  {k}: {str(v)[:100]}")
