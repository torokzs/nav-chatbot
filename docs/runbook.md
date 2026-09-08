# Operációs runbook

Ez a runbook a NAV Információs Asszisztens üzemeltetéséhez, újraindexeléséhez és hibaelhárításához ad gyakorlati lépéseket.

## 1. Fabric tenant beállítások

A következő lépések jelenleg manuálisak a Fabric admin / workspace oldalon:

1. **Admin Portal** → engedélyezd: **Users can create Fabric items**.
2. A cél workspace-ben kapcsold be a **Workspace identity** funkciót.
3. Rendelj a workspace-hez **Fabric capacity**-t.
4. Ellenőrizd, hogy a notebook futtató identitás hozzáfér:
   - Lakehouse Files/Tables
   - Azure AI Document Intelligence
   - Azure AI Foundry
   - Azure AI Search

### RBAC hozzárendelések (Azure-oldali)

A notebook-futási felhasználónak (user principal) az alábbi RBAC szerepkörökre van szüksége:

| Erőforrás | Szerepkör | Miért |
|-----------|-----------|-------|
| Cognitive Services (cog-*) | `Cognitive Services User` | Embedding API hívások |
| Document Intelligence (di-*) | `Cognitive Services User` | PDF parse REST hívások |
| AI Search (srch-*) | `Search Service Contributor` | Index létrehozás/kezelés |
| AI Search (srch-*) | `Search Index Data Contributor` | Dokumentum feltöltés |

### Autentikáció a notebookban

- **Document Intelligence és Embedding**: `mssparkutils.credentials.getToken("https://cognitiveservices.azure.com")` — működik
- **AI Search**: `mssparkutils.credentials.getToken("https://search.azure.com")` **NEM működik** (HTTP 500). Workaround: **Search Admin Key** használata env változóból
- **Fájlok olvasása**: `mssparkutils.fs.head()` bináris fájlokra korrumpál! Helyette: `/lakehouse/default/Files/raw/<adoev>/` filesystem mount Python `open()`-nal

### Rate limitek

- `text-embedding-3-large` S0 szinten: batch size 8, exponenciális backoff, ~25 perc teljes embed (2180 chunk + 82 doc)
- Document Intelligence: 82 PDF parse ~14 perc, ritka 429-es hiba retry-vel kezelhető

## 2. Fabric kapacitás kezelése

### Meglévő kapacitás felélesztése (resume)

Portalból vagy CLI-ból:

```bash
az resource invoke-action \
  --action resume \
  --ids /subscriptions/<subId>/resourceGroups/<rg>/providers/Microsoft.Fabric/capacities/<capacityName>
```

### Kapacitás felfüggesztése (pause / suspend)

```bash
az resource invoke-action \
  --action suspend \
  --ids /subscriptions/<subId>/resourceGroups/<rg>/providers/Microsoft.Fabric/capacities/<capacityName>
```

### F4 kapacitás létrehozása, ha még nem létezik

A repo Bicep modulja F4 SKU-val számol. Javasolt út:

```bash
azd up
```

Ha külön ellenőriznéd az erőforrást:

```bash
az resource list \
  --resource-group nav-chatbot-rg \
  --resource-type Microsoft.Fabric/capacities \
  -o table
```

## 3. Notebook futtatás sorrendje

Az ingest/index pipeline ajánlott futási sorrendje:

1. `fabric/notebooks/01_ingest_pdfs.py`
2. `fabric/notebooks/02_parse_chunk.py`
3. `fabric/notebooks/03_embed_index.py`
4. `fabric/notebooks/04_eval_notebook.py`

Ajánlott ellenőrzési pontok:

- `01` után: a PDF-ek megjelentek a `Files/raw/2021/`–`Files/raw/2026/` alatt
- `02` után: létrejött `nav_chunks` és `nav_documents` Delta tábla
- `03` után: az AI Search indexek és synonym map frissültek
- `04` után: létrejött az `eval_results` tábla és van összesített metrika

## 4. AI Search index újraépítése

### Mikor szükséges?

Index újraépítés kell, ha:

- új NAV füzet érkezik,
- változik a chunking logika,
- módosul a mezőséma,
- változik a synonym map,
- változik az embedding modell vagy dimenzió,
- sérült / hiányos indexszinkron gyanúja merül fel.

### Hogyan történjen?

1. Futtasd újra a `02_parse_chunk.py` notebookot.
2. Ezután futtasd a `03_embed_index.py` notebookot.
3. Ellenőrizd, hogy:
   - a synonym map elkészült,
   - a `nav-chunks` és `nav-documents` index frissült,
   - a dokumentum- és chunk-számok egyeznek az elvárttal.
4. Végül futtasd a `04_eval_notebook.py` notebookot.

Megjegyzés: a `03_embed_index.py` notebook `create_or_update_index` hívással dolgozik, tehát sémafrissítésnél is ez a belépési pont.

## 5. Container Apps revízió kezelése

A backend Container App **multiple revisions** és **scale-to-zero** módban fut. A
`minReplicas: 0` csökkenti az üresjárati költséget, de az első kérés hidegindítást
okozhat.

A deployment folyamat:

1. megtartja a forgalmat kapó stabil revíziót,
2. `staging` label alatt teszteli a candidate revíziót,
3. csak a quality gate után ad neki 100% forgalmat,
4. siker után deaktivál minden korábbi revíziót,
5. sikertelen quality gate esetén deaktiválja a 0% forgalmú candidate revíziót.

### Aktuális revíziók listázása

```bash
az containerapp revision list \
  --name <backend-app-name> \
  --resource-group nav-chatbot-rg \
  --query '[].{Name:name,Active:active,Traffic:trafficWeight}' \
  -o table
```

### Manuális traffic shift

Példa 90/10 megosztásra:

```bash
az containerapp ingress traffic set \
  --name <backend-app-name> \
  --resource-group nav-chatbot-rg \
  --revision-weight <stable-revision>=90 <candidate-revision>=10
```

### Rollback előző revízióra

Az automatikus cleanup deaktiválja a korábbi revíziókat. Rollback előtt aktiváld a
kiválasztott revíziót, majd állítsd át a forgalmat:

```bash
az containerapp revision activate \
  --name <backend-app-name> \
  --resource-group nav-chatbot-rg \
  --revision <previous-revision>

az containerapp ingress traffic set \
  --name <backend-app-name> \
  --resource-group nav-chatbot-rg \
  --revision-weight <stable-revision>=100
```

### Revízió ellenőrzés deployment után

- `/health` endpoint válaszol-e
- SSE chat stream elindul-e
- nincs-e 5xx spike az Application Insightsban
- a latency romlott-e az előző revízióhoz képest
- csak a 100% forgalmat kapó revízió aktív-e

```bash
az containerapp revision list \
  --name <backend-app-name> \
  --resource-group nav-chatbot-rg \
  --query '[].{Name:name,Active:properties.active,Traffic:properties.trafficWeight,Replicas:properties.replicas}' \
  -o table
```

### Egyszeri régi revízió-cleanup

Az első költségoptimalizált deployment előtt a régi, 0% forgalmú revíziókat
deaktiválni kell. A forgalmat kapó revízió nevét mindig ellenőrizd, és azt ne
deaktiváld.

```bash
az containerapp revision deactivate \
  --name <backend-app-name> \
  --resource-group nav-chatbot-rg \
  --revision <zero-traffic-revision>
```

## 6. Hibaelhárítás

### Document Intelligence timeout

Tünetek:

- hosszú futás
- 408 / 429 / 5xx hibák
- részleges output vagy hiányzó chunkok

Teendők:

1. kisebb PDF batch-csel futtasd újra,
2. ellenőrizd a forrásfájlok méretét és minőségét,
3. futtasd újra csak a hibás PDF-ekre,
4. figyeld a retry mintát és a per-file hibalistát.

### Embedding rate limit

A notebook exponenciális backoffot használ; ha így is fennáll:

1. csökkentsd az `EMBED_BATCH_SIZE` értékét,
2. futtasd újra később,
3. ellenőrizd az Azure AI Foundry kvótát és régiós elérhetőséget.

### AI Search sync failures

Tipikus okok:

- hibás Delta tábla séma,
- hiányzó mező,
- JSON parse gond a `hivatkozott_fuzetek` mezőben,
- vektordimenzió eltérés.

Teendők:

1. ellenőrizd a `nav_chunks` és `nav_documents` táblák állapotát,
2. hasonlítsd össze a táblaoszlopokat az indexsémával,
3. szükség esetén futtasd újra a `03_embed_index.py` notebookot,
4. súlyos sémadrift esetén index rebuild javasolt.

## 7. Monitoring

### Application Insights lekérdezések

### 5xx hibák a backendben

```kusto
requests
| where timestamp > ago(24h)
| where resultCode startswith "5"
| project timestamp, name, resultCode, duration, operation_Id
| order by timestamp desc
```

### Lassú kérések

```kusto
requests
| where timestamp > ago(24h)
| summarize p50=percentile(duration, 50), p95=percentile(duration, 95), p99=percentile(duration, 99) by name
| order by p95 desc
```

### Kivételnapló a chat útvonalra

```kusto
exceptions
| where timestamp > ago(24h)
| project timestamp, type, outerMessage, operation_Id
| order by timestamp desc
```

### Trace-ek semantic fallback / retrieval hibák kereséséhez

```kusto
traces
| where timestamp > ago(24h)
| where message has_any ("Semantic search unavailable", "Section expansion failed", "Chat request failed")
| project timestamp, severityLevel, message
| order by timestamp desc
```

### Figyelendő kulcsmetrikák

- API latency (p50 / p95 / p99)
- 4xx és 5xx error rate
- token usage / LLM költség
- AI Search query latency
- Document Intelligence retry arány ingest közben
- Container Apps replica count és restart szám

## 8. Költség-kontroll

Ajánlott rutinok:

- **Fabric capacity pause**, amikor nincs ingest vagy eval futás
- **Container Apps scale-to-zero** és a 0% forgalmú aktív revíziók megszüntetése
- AI Search tárhely-, vektor- és throttling limitek ellenőrzése
- csak szükség esetén teljes újraindexelés
- smoke eval futtatása teljes eval előtt
- App Insights retention és lekérdezési volumen figyelése

Példa Container Apps skálázásra:

```bash
az containerapp update \
  --name <backend-app-name> \
  --resource-group nav-chatbot-rg \
  --min-replicas 0 \
  --max-replicas 3
```

Az AI Search Basic célkonfiguráció 1 replika × 1 partíció. A jelenlegi két index
jóval a Basic limitek alatt van, de minden újraindexelés után ellenőrizd:

```bash
az monitor metrics list \
  --resource <search-service-resource-id> \
  --metrics IndexStorageUsage IndexVectorUsage ThrottledSearchQueriesPercentage \
  --interval PT1H \
  --aggregation Average Maximum
```

Havi költségellenőrzéskor külön figyeld:

- aktív Container Apps revíziók száma,
- 0% forgalmú, mégis aktív revíziók,
- Container Apps active/idle vCPU és memória,
- AI Search tier és Search Unit szám,
- Log Analytics adatbefogadás.

## 9. Javasolt üzemeltetési rutin

### Napi

- App Insights hibák és latency áttekintése
- Container App revízió- és egészségellenőrzés

### Heti

- költségellenőrzés
- aktív és 0% forgalmú Container Apps revíziók ellenőrzése
- AI Search storage/vector usage és throttling ellenőrzése
- retrieval minőség smoke eval alapján
- index és Delta táblák konzisztenciaellenőrzése

### Új füzet publikálásakor

1. ingest
2. parse + chunk
3. embed + index
4. smoke eval
5. teljes eval
6. szükség esetén canary rollout, majd traffic shift

### Többéves kiadás

1. Futtasd az `01` és `02` notebookot külön-külön minden támogatott évre a `TAX_YEAR=2021`–`2026` paraméterrel.
2. Ellenőrizd, hogy minden Delta sor rendelkezik `adoev` mezővel, és nincs ismétlődő `document_id` vagy `chunk_id`.
3. Futtasd a `03` indexelést a közös Delta táblákra.
4. Futtasd a backend/frontend unit teszteket, az integration smoke-ot és a többéves evalt.
5. A PR megnyitása és minden új push automatikusan elindítja a `test` deploymentet.
6. A workflow buildet, provisionálást, deployt, integration smoke-ot és LLM evalt futtat.
7. A PR csak a teljes quality gate sikeres futása után merge-elhető; production deployment ebben a fázisban nincs.
