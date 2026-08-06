# Azure AI modell benchmark

A `.github/workflows/model-benchmark.yml` egy kizárólag kézzel indítható,
költségkorlátos benchmark. Az aktuális Azure AI Foundry erőforrás modelljeiből
dinamikusan állít össze nyolc slotot: OpenAI és Claude frontier referencia,
két-két költséghatékony OpenAI és Claude, egy Mistral és egy DeepSeek. A nem
elérhető slotok `skipped` állapotban jelennek meg, és nem állítják meg a többi
jelöltet.

## Indítás

1. Nyisd meg az **Actions > Model benchmark > Run workflow** oldalt.
2. Válaszd ki a GitHub environmentet.
3. Szükség esetén írd felül az erőforrásokat vagy a judge deploymentet.
4. Ellenőrizd a `max_estimated_cost_usd` keretet; alapértéke 100 USD.

A workflow csak `workflow_dispatch` triggert tartalmaz. Nem indul pushra,
pull requestre vagy ütemezésre, és nem módosítja automatikusan a produkciós
modellt.

## GitHub konfiguráció

Az environmentben az alábbi secret-ek szükségesek az OIDC bejelentkezéshez:

| Név | Leírás |
|---|---|
| `AZURE_CLIENT_ID` | Federált service principal kliensazonosító |
| `AZURE_TENANT_ID` | Entra tenant |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription |

Az alábbi environment variable-ek adják az alapértékeket. A kézi inputok az
erőforrás- és judge-beállításokat biztonságosan felülírhatják.

| Név | Kötelező | Leírás |
|---|---:|---|
| `AZURE_RESOURCE_GROUP` | igen | Foundry account és Container App resource group |
| `AZURE_AI_ACCOUNT_NAME` | igen | Azure AI Foundry/Cognitive Services account |
| `AZURE_CONTAINER_APP_NAME` | igen | Produkciós backend Container App |
| `AZURE_LOCATION` | igen | Retail ár- és modellrégió |
| `AZURE_AI_JUDGE_DEPLOYMENT` | igen | Fix, erős, a jelöltektől független judge deployment |
| `AZURE_OPENAI_ENDPOINT` | nem | Judge endpoint; hiányában az account endpointja |
| `AZURE_AI_PROJECT_ENDPOINT` | nem | Foundry projekt URL cloud eval naplózáshoz |

Az accountonkénti concurrency group megakadályozza, hogy ugyanarra a Foundry
erőforrásra két benchmark egyszerre telepítsen modelleket.

## Azure jogosultságok

Az OIDC service principalnak minimálisan olvasnia kell a resource groupot,
kezelnie kell az account model deploymentjeit, és másolnia/deaktiválnia kell a
Container Apps revíziókat. Tipikus beépített szerepkörök:

- `Reader` a resource groupon;
- `Cognitive Services OpenAI Contributor` a Foundry accounton;
- `Container Apps Contributor` a backend Container Appon vagy resource groupon.

A judge hívásához az identitásnak inference jogosultság is kell
(`Cognitive Services OpenAI User`), ha nem kulcsalapú hitelesítés történik. A
backend benchmark-revíziója a produkciós revízió identitását örökli. Az Azure
Retail Prices API publikus, ahhoz nem szükséges Azure szerepkör.

## Folyamat és biztonsági korlátok

1. Az Azure CLI lekéri a régióban elérhető modellverziókat, realtime SKU-kat,
   kapacitásadatokat és account usage/quota adatokat.
2. Az Azure Retail Prices API input/output token-méterei alapján ár kerül a
   modellekhez és a judge-hoz. Hiányzó vagy többértelmű meter esetén az ár
   `unknown`; az érintett jelölt fizetős futása kimarad. Ismeretlen judge-árnál
   a teljes paid benchmark blokkol, mert a judge-költség nem korlátozható.
3. A kiválasztott számú jelölt-hívás és a kérdésenkénti három LLM judge értékelés
   konzervatív becslése lefut **minden fizetős hívás előtt**. Ismeretlen ár vagy
   a keret túllépése esetén a benchmark blokkol, deploymentet nem hoz létre.
4. Jelöltenként run-scoped deployment készül, majd a stabil backend revízió
   másolata kizárólag az `AZURE_AI_FOUNDRY_CHAT_DEPLOYMENT` változó
   felülírásával.
5. A revízió saját FQDN-jén lefut az `evals/qa.jsonl` kiválasztott kérdésszáma.
6. Az Azure AI Evaluation SDK a fix judge deploymenttel groundedness,
   relevance és fluency score-t számol; a citation correctness determinisztikus
   evaluatorból érkezik.

A benchmark kliens kérésenként opt-in módon bekéri az SSE streamben a tényleges
retrieval contextet, így a groundedness judge ugyanazt a forrásanyagot látja,
amelyből a válasz készült. Normál chatkérésnél ez az extra esemény nem jelenik
meg. A hívások alapértelmezésben 25 kérés/perc ütemezéssel futnak, ezért az
örökölt backend rate limit nem torzítja a jelöltek hibaarányát vagy latency
adatait.

A benchmark csak `Multiple` Container Apps revision mode mellett indul, és
megköveteli, hogy a produkciós ingress explicit revízióra mutasson.
`latestRevision` vagy label-only traffic rule esetén fail-closed módon leáll,
mert az új benchmark-revízió különben éles forgalmat kaphatna. A benchmark
soha nem ír át traffic weightet.

A Container Apps revíziók létrehozása, állapotellenőrzése és deaktiválása
közvetlen ARM REST-hívással történik, ezért a futás nem függ a `containerapp`
Azure CLI extension verziójától.

Az operátori ellenőrzéshez a `--preflight-only` kapcsoló a teljes modell-,
SKU-, quota- és Retail Price felderítést, valamint a költségkaput lefuttatja,
de nem hoz létre deploymentet és nem indít fizetős modellhívást.

A `question_count` workflow input (CLI-n `--question-count`) 1 és 100 közötti
determinista kérdésszámot választ. Az 5 kérdéses gyors mód az első öt kanonikus
kérdést használja, ezért futások között közvetlenül összehasonlítható; az ilyen
kis minta iránymutató, nem helyettesíti a teljes minőségi benchmarkot.

A `candidate_models` alapértéke `model-router`, így a workflow a base revision
aktuális deploymentjét hasonlítja össze a valódi Azure Model Routerrel. Vesszővel
további pontos katalógusmodell-nevek adhatók meg; üres érték visszakapcsolja a
teljes családalapú shortlistet. A dinamikus router tényleges díja a választott
almodelltől függ, ezért a preflight az operátor által megadott
`dynamic_model_max_cost_per_question_usd` plafont használja.

A gyors mód alapértelmezett ideiglenes kapacitása 100 egység, de a felderített
szabad kvóta ezt továbbra is korlátozza. A benchmark a deployment létrehozása után
külön data-plane próbával megvárja az Azure legfeljebb néhány perces
propagációját, és csak ezután indítja a backend revíziót és a mért kérdéseket.

PR előtti méréshez a `base_revision` inputtal megadható egy előzőleg
deployolt, egészséges, 0%-os forgalmú branch-revízió. Így a benchmark a friss
backend-kódot méri, miközben a produkciós traffic rule változatlan marad.
Ennek a revíziónak az aktuális chat deploymentje mindig külön
`current-model` baseline jelöltként fut, ideiglenes deployment létrehozása nélkül.

A retrieval modellfüggetlen: a query rewrite után az embedding deployment és az
Azure AI Search végzi. A benchmark ezért kérdésenként egyszer, a base revisionön
futtatja, majd ugyanazt a befagyasztott kontextust adja minden jelölt run-scoped,
titokkal védett revíziójának. A normál chat nem fogad el context override-ot.
A citation evaluator a teljes befagyasztott kontextus metadataját ellenőrzi, nem
a felhasználói felülethez három elemre korlátozott forráslistát.

Az `evals/qa.jsonl` és `evals/qa.smoke.jsonl` kizárólag 2026-os ground truthot
tartalmaz. Ezt az `adoev` és `ground_truth_tax_year` mező is explicit jelzi; a
füzet- és oldalhivatkozások a 2026-os Azure Search indexhez vannak igazítva.

## Metrikák és rangsorolás

Kérdésenként rögzül a válasz, a forráslista, a hiba, TTFT, teljes latency,
becsült output token és token/másodperc. Modellenként mean, p50 és p95 készül.
Az SSE válasz jelenleg nem ad provider usage adatot, ezért az output token
`cl100k_base` becslés. Az input és judge tokenek konfigurált konzervatív
feltételezések; ezt a JSON riport is jelöli.

Csak a 100 kérdést hiba nélkül teljesítő, ismert árú és minden küszöböt elérő
modell ajánlható:

| Metrika | Minimum |
|---|---:|
| Groundedness | 4.0/5 |
| Relevance | 4.0/5 |
| Fluency | 4.0/5 |
| Citation correctness | 0.8 |

A legalacsonyabb becsült költség nyer. Holtversenynél a magasabb összesített
minőség, majd az alacsonyabb p95 teljes latency dönt.

## Artifactok és takarítás

Az `evals/benchmark-results` könyvtár tartalmazza a gépi JSON riportot, a
Markdown rangsort, a jelöltenkénti kérdés- és Evaluation SDK eredményeket,
valamint a run-scoped erőforrás-regisztert. A Markdown a GitHub job summaryba
is bekerül, a teljes könyvtár 30 napos artifactként feltöltődik.

Minden jelölt után célzott deaktiválás és deployment-törlés fut. A Python
orchestrator `finally` blokkja, majd egy külön GitHub Actions `if: always()`
cleanup lépés ismét ellenőrzi a regisztert. Csak az adott run által regisztrált
benchmark deploymentek és revíziók kerülnek takarításra.

Ismert korlátok:

- egyes marketplace modellek első deploymentje külön feltétel-elfogadást
  igényelhet; az érintett jelölt hibás lesz, a többi tovább fut;
- a Retail Prices elnevezése eltérhet a model catalog nevétől; bizonytalan
  egyezés nem kap becsült árat, ezért az érintett jelölt futása kimarad;
- a benchmark nem keres másik régiót vagy másik Foundry accountot;
- a judge deploymentet előre, a benchmark jelöltjeitől függetlenül kell
  létrehozni és változatlanul tartani az összehasonlíthatóság érdekében.
