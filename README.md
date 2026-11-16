# Brand Discovery Engine 0.6.5

## Start here (Windows + VS Code)

1. Clone or extract this repository to a NEW folder, such as `Documents\BrandDiscoveryEngine-v0.6.5`. Do not overwrite a previous project folder.
2. In VS Code, choose **File > Open Folder** and select that folder.
3. Open VS Code Terminal and run `START_BRAND_ENGINE.bat` (or double-click the file in File Explorer). The launcher creates `.venv`, installs dependencies, and opens Streamlit locally.
4. Alternatively, use `python -m venv .venv`, `.venv\Scripts\python -m pip install -r requirements.txt`, and `.venv\Scripts\python -m streamlit run app.py`.
5. The app normally opens at `http://localhost:8501`. This local URL is **not accessible from your iPad** without additional networking/hosting.
6. Existing saved projects are stored in your Windows AppData BrandDiscoveryEngine directory and may reopen automatically. Set `BRAND_ENGINE_DATA_DIR` to a new disposable directory before starting if you want an isolated installation.

## Included changes since v0.6.2

- Current application version is 0.6.5.
- First-time users get a fictional sample business project. Existing saved projects continue to reopen unchanged.
- Outdated **industry-search** results are excluded from conflict leads after research terms change. Old exact/broad results are still shown as leads; rerun research to update all searches.
- API keys may be supplied using `SERPAPI_KEY` and `YOUTUBE_API_KEY` environment variables for hosted deployments; environment variables take precedence over local keyring. Never commit keys.
- Automated conflict assessments produce screening recommendations only. Only explicit user actions save Keep, Investigate or Discard decisions. Historical saved choices, including automatic or unknown-provenance choices, are preserved unchanged.
- Startup, dashboard filters, and decision changes use saved evidence only. Automated provider searches run only after selecting **Research selected names**.
- Added focused regression tests for stale industry evidence and environment credentials.

## Important limitations

This is **not** a trademark clearance tool. Search evidence is heuristic, not an AI semantic interpretation. RDAP 'Potentially available' means only that the registry did not return a registration record; confirm purchase with a registrar. Check USPTO and other jurisdictions separately. No live domain, trademark or SerpAPI searches were performed when packaging this release.

## Milestone 1: research reliability and data protection

- Name matching respects word boundaries and normal brand separators. A title
  such as "Smith & Harbor: Sketch History of Our Accounting Software" does not
  become a HarborSketch brand conflict merely by removing punctuation. Strong
  leads also require brand identity, relevant context and the existing entity-page
  checks. These remain heuristics, not legal clearance.
- Valid empty search results are completed evidence. Missing/malformed results,
  authentication errors, quota limits and connection failures are not empty
  successes. A narrowly recognized successful SerpAPI no-results envelope is
  supported; arbitrary provider errors remain failures.
- Structured RDAP no-record responses and genuinely empty 404s from the known
  Verisign `.com`/`.net` endpoints mean **Potentially available**, never verified
  purchase availability. HTML/proxy errors, malformed replies, mismatched domains
  and rate limits remain unknown/failed.
- The displayed research combines the latest successful evidence **per source**
  for the current targets and industry terms. A failed refresh retains earlier
  evidence, displays the later failure, and stays queued for retry. A successful
  empty refresh can supersede earlier matches. Original runs are not rewritten.
  **Research evidence dates (UTC)** shows source dates in Advanced review; retained
  evidence warnings also appear on the main candidate card and in exports.
- **New and incomplete only** retries failed, missing or changed-context checks,
  reusing completed checks, including completed zero-result searches. **All
  (force refresh)** deliberately rechecks every enabled source. Research stops
  on a failed save checkpoint and reports only confirmed saved candidate counts.
- Projects retain the existing JSON format and atomic `os.replace` writes.
  Loaded projects carry an in-memory content revision; an exclusive per-project
  save lock serializes revision checking and replacement. A stale browser cannot
  silently overwrite a different saved revision. Save failures do not advance
  the saved snapshot or timestamp, and working edits remain recoverable.
- On a save error use **Download unsaved project backup** before closing or
  reloading. **Retry project save** helps with transient disk errors. For revision
  conflicts, use **Reload saved database**, explicitly confirm discarding the
  local working copy, then reconcile your downloaded backup manually. Reloading
  resets the budget widget so stale UI state does not silently re-save old data.
  Unsubmitted browser-form edits are not part of a server-side backup.
- A successful project save with a failed last-open-settings save is reported
  separately. Revision protection covers project databases, not concurrent edits
  to display-name/last-open settings or external programs that ignore the lock.
  A process crash can leave `database.json.lock`; after closing all app sessions
  and retaining backups, remove only that database's orphan lock before retrying.
  Never remove or replace the project JSON to resolve a lock.

  The local `.gitignore` excludes JSON, CSV and workbook files to protect project
  data and exports, except for the included blank workbook template. It also excludes
  common credentials, local environments and review archives. Review and explicitly
  allowlist any future non-sensitive configuration or fixture before adding it.
  Ignore rules are not a substitute for checking staged files and scanning for secrets.

No historical decision migration, pricing/ranking-policy change, finalist
selection, external research, deployment or new integration is part of this
milestone. Library callers should retain the project object returned by
`new_project`/`load_project`; an untracked plain dictionary cannot replace an
existing database.

Run the offline regression suite with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Fixtures cover matching, empty/error responses, retained per-source evidence,
selective retries, decision preservation, simultaneous/stale saves, atomic-write
failures, recoverable UI edits and research checkpoint failures. Live provider
behavior, real iPad Safari and remote networking still require separate validation.

## Saved project databases and friendly names

The sidebar **Saved projects** selector lists each readable project JSON with its
filename, candidate count, existing project title and actual last-modified date
(UTC). Unreadable files are reported and left untouched. The **Active database**
banner and main **Active project** caption identify the file currently open;
selecting a different entry alone does not load or change it.

Select a database, enter a **Friendly display name**, and click **Save display name**.
For example, use **Project A** and **Project B** to distinguish
datasets. Clear the friendly name to use its original project title again.
Names are local display-only metadata stored in `settings.json` under
`project_display_names`, keyed by exact database filename. Naming never changes
the project title, filename, modification date, candidates, research, decisions,
GoDaddy cache, pricing history or manual domain observations.

Click **Load selected project**, acknowledge the switch warning, then click
**Confirm database switch**. **Cancel database switch** keeps the current project
and its unsaved working edits open. Save unfinished project/review/listing forms
before confirming: Streamlit cannot reliably detect unsubmitted client-side form
edits, so every switch requires explicit confirmation, even when no unsaved
changes are detected. New-project creation and restoring a backup as a new
project use the same confirmation protection. Switching reopens the target file
without rewriting it; normal explicitly saved edits still update that file.
The exact selected filename is remembered for the next application start.

Existing storage is unchanged: `%APPDATA%\BrandDiscoveryEngine\projects` on Windows,
or the directory configured by `BRAND_ENGINE_DATA_DIR`. Friendly names and last-open
selection live in the parent directory's `settings.json`, not in exported project
JSON backups. Back up that settings file separately if you want to preserve names.
No databases are automatically merged, deleted, renamed or overwritten by these
controls. Existing legacy-project migration remains unchanged.

## Domain pricing and the $100 ceiling

Open **Domain pricing (read-only)** above the existing Quick Overview:

1. **Maximum domain acquisition price (USD)** defaults to **$100** per project.
   You can lower it, but cannot raise it beyond the firm $100 limit.
2. Leave **Check only shortlisted names (Keep + Investigate)** checked to limit
   pricing to the existing shortlist, or uncheck it for all candidate `.com` targets.
   Saved domain target overrides are respected. Invalid/non-`.com` targets are reported and skipped.
3. The displayed count tells you how many distinct domains will be requested and
   how many saved quotes will be reused. Click **Check Domain Prices** to run only
   **GoDaddy v3 batch lookups** using the existing local token. Keep shortlist-only
   checked to limit checks to shortlisted names.
   This does not run research, DNS/RDAP, Google, YouTube or SerpAPI.
4. Choose **Show names > Affordable Domains** to see only available `.com` domains with fresh,
   **final all-in verified** USD acquisition totals at or below your budget (never above $100). This filter
   includes all qualifying candidates, even a manually discarded candidate;
   it does not change any decision. **Shortlist (Keep + Investigate)** remains
   the decision-based view and includes over-budget and unpriced names.
5. Both views have a sortable/selectable table with domain, registration availability,
   premium status, registration/base price, separate premium/resale purchase fee,
   total quoted acquisition price, annual renewal price, budget classification,
   check timestamp, currency, inventory, source and pricing notes. Quick Overview
   also includes **Domain pricing details**; Detailed Results remains.
   Prices are also included in the existing CSV/workbook exports and JSON backups.

**Domain Pricing Results** now appears directly below **Check Domain Prices** in
the initially expanded pricing section. It displays the active project's saved
cache without needing a new lookup or switching the brand shortlist view.
Summary counts are per distinct cached domain, independent of Keep/Investigate/Discard.
The default **All checked domains** view includes stale and failed-refresh records.
**Provisional under $100** sorts quoted acquisition totals low to high (at most
$100, subject to a lower project budget). Other views include **Stale quotes**,
**Verified affordable**, **Over budget**, and
**Unavailable or unknown price**. All checked domains puts provisional quotes
first, then sorts by quoted price; missing prices sort last within each group.

Use **Search pricing results** to find a candidate, domain or notes. **Download
pricing results CSV** exports the displayed filtered rows, including dates,
price reliability, currency and source. Unknown amounts remain blank. The CSV
escapes spreadsheet formulas in candidate names and notes. No token is exported.
Multiple candidates sharing a domain appear together in one row. Cached domains
no longer matching a current candidate target are retained and marked **not in
current candidate list**. Stale quotes remain visible but cannot count as affordable
or provisional under budget. Quotes previously over budget are labeled as stale
and over budget at the time of observation, not as current offers. A zero
provisional count does not mean saved amounts were lost. Expiry never clears saved price fields.
No search, filter, download or rerun sends an API request.

On an iPad-sized screen, the pricing table puts **Candidate name**, **Quoted total
(USD)** and **Budget classification** first. Candidate names stay pinned while
you swipe horizontally to see other columns; rows have larger touch targets.
Pricing and project action buttons fill their available width. Open the sidebar
using the top-left arrow to select/name projects, then collapse it for more table
space. Landscape orientation also provides more room.

An asking price written only in candidate review notes is preserved as a note;
it does not automatically become a verified structured quote. Use **Listing
candidate** and **Save observed listing** to record a currently verified price,
such as `1750` (without `$` or commas), registration status and listing source.
Leave unknown renewal amounts blank. A current structured $1,750 resale asking
price is **Over budget**, never Affordable, even without a verified tax-inclusive
total. Saving/reverifying an observed listing retains the previous cached quote
in `domain_pricing_history` when its substantive details change; API refreshes do not overwrite manual listings.
No existing notes are automatically converted or datasets modified.

### Remote access readiness

The application runs on the Windows computer; `localhost:8501` on an iPad refers
to the iPad, not that computer. Remote access still requires an explicitly
configured secure private connection (for example, a trusted VPN) or an
authenticated hosting arrangement. Streamlit's default local launch does not
provide that setup or user authentication. Do not expose the app directly to
the public internet or forward its port without access controls. Keep the
Windows host running. This update does not deploy, open firewall ports or
configure remote access. Tablet-sized desktop-browser checks do not substitute
for a final Safari/iPad test through your chosen secure connection.

The active project is selected by its exact saved filename (legacy selection uses
its project ID), not its title or the
size of a previously discussed dataset. The sidebar's **Load selected project**
switches explicitly; no pricing filter reduces or rewrites the candidate list.

Routine DNS, registry and pricing screening only checks `.com` targets. Historical
other-extension records remain saved and visible but do not influence eligibility
or recommendations. A registered `.com` without a resale quote remains Investigate
and ranks lower; it is never automatically discarded for registration alone.
Unknown, stale and failed-refresh prices also require investigation. A fresh
under-budget provisional quote is promising, not a verified checkout total.
Only the user changes a saved Keep, Investigate or Discard decision.

Availability never proves affordability. Classifications are **Affordable**,
**Over budget**, **Premium - price unknown**, **Registered - resale unknown**, and
**Price unavailable**, plus **Provisional - total unverified**. Registration quotes and resale quotes are distinct; a registered
domain never receives the standard registration tariff as its asking price.
Premium status, acquisition and renewal are independent; renewal is not the acquisition price.
An API premium price of zero is a placeholder, not a free domain.
GoDaddy quotes below the budget are provisional because taxes and ICANN charges
are not supplied. They **never** enter Affordable Domains automatically. Known
quoted amounts or known minimum components above the budget are **Over budget**,
even when some charges remain unknown. Unavailable domains and incomplete premium
quotes cannot be affordable. A premium marketplace listing is shown as registered
and priced for resale, not available for standard registration.

The last usable quote is saved in the optional `domain_pricing` map, keyed by
domain. `domain_pricing_attempts` records the latest attempt, its date and any
safe-to-display error. An incomplete result or failed request does not replace
the last usable quote. A newer unavailable result is shown as the latest status,
while the prior price remains visible as historical evidence. A separate `domain_pricing_tariff` caches
Namecheap's account-specific standard `.com` registration/renewal tariffs only.
GoDaddy never calls that tariff API or substitutes a standard tariff for a resale.
Old JSON projects
need no migration. Quotes and recent unsuccessful attempts are reused for 24 hours, and never
trigger requests on reruns or startup. After 24 hours the historical quote remains
visible but does not qualify as Affordable; explicitly click the button to update it.
**Refresh cached prices** explicitly rechecks even fresh cached API domains.
User-observed listings are authoritative and never overwritten by API checks,
even when stale; use the listing form to reverify/update them.
API checks replace pricing only, never research or decisions.
Fresh quotes from another provider are not mistaken for GoDaddy cache entries.
When substantive quote details change, the previous record is preserved under
`domain_pricing_history`; repeated identical quotes do not add duplicate history.
An explicit refresh retries recent failures and incomplete attempts.
Manual listing overrides stay authoritative.
Successful batches are saved immediately; failures stop further calls and show an error.
No candidates, research runs or saved reviews are deleted or rewritten by pricing.

### Active provider: GoDaddy v3

The application reuses the standalone probe's token loader and price parsing.
Your existing `.env.godaddy` is used automatically on button click; no token
needs to be entered in Streamlit or chat. `GODADDY_PAT` in the process environment
takes precedence. The token is never put into session state, project JSON, CSV,
workbooks, logs or error messages. `.env.godaddy` and `.env` are Git-ignored;
this was verified with Git's `check-ignore`. Do not include secret files in shared ZIPs.

The adapter calls only **POST /v3/domains/check-availability**, the documented
read-only batch operation (not a purchase or modification). Batches contain at
most 25 domains, so 51 distinct uncached shortlist domains take three requests.
Batch responses use the documented `{"items": [...]}` envelope, with requested
domain identities validated before saving.
Requests use `optimizeFor=ACCURACY`, 4-second connect/12-second read timeouts,
no redirects and **no automatic retries**. Progress reports checked/saved domains.
Successful batches persist immediately; request errors stop later batches.
Domain-level errors are saved as unknown with an explicit note/warning.
Requests are spaced at least 2.5 seconds apart, and a zero remaining quota stops
the next batch. HTTP 429 reports a numeric provider wait interval when supplied;
retry only via a later explicit button click. Other clients share token limits.

All prices use the currency's minor-unit scale; USD values are divided by 100.
The annual registration/base price and each returned premium purchase fee are
summed in the same currency. Unsupported/mixed currencies and missing components
are never treated as verified USD acquisition totals. Missing renewal is unknown,
not assumed equal to registration. Only the one-year term is displayed as annual.

ICANN fees and tax are **not returned by this endpoint**. Returned premium fees
are included, but the quoted total stays provisional. To qualify for Affordable
Domains, use the observed-listing form to record an independently verified final
all-in total and check **I verified the final all-in acquisition total, including
tax, ICANN and all mandatory charges**. This is only a manual verification record,
not an instruction or mechanism to purchase. Old saved listing quotes without this
confirmation remain visible but provisional if under budget.

No additional live API calls were made while integrating or testing this change.
The earlier three-domain GET probe established token access; the batch workflow
is verified with mocked responses only. Live batch response/permission behavior
may still vary; any mismatch stops instead of silently substituting a guessed price.

### Preserved adapter: Namecheap (optional live activation)

The existing registrar links already point to Namecheap. Its production
`namecheap.domains.check` method reports registration availability, `IsPremiumName`,
premium registration/renewal prices, ICANN and early-access fees. Standard
one-year USD prices come from `namecheap.users.getPricing`; availability alone
never substitutes for that tariff. Missing prices, fee fields or unsupported currencies
remain unknown. Its two read-only API methods remain implemented, over HTTPS,
with 4-second connect/12-second read timeouts and redirects disabled.
Lookups batch up to 50 domains. Calls are spaced at least 1.3 seconds apart;
HTTP 429 and XML/API errors stop rather than automatically retrying.
Namecheap's documented key-wide limits are **50/minute, 700/hour, 8,000/day**;
other applications using the same key share those limits.

**No Namecheap live requests were made during implementation/testing.**
Namecheap documents no additional API fee, but production activation requires
at least one of: **20 domains in the account**, **$50 account balance**, or
**$50 spent during the previous two years**. Support may consider exceptions.
Do not deposit funds, purchase domains, or pay for access for this feature.
Sandbox accounts do not provide real prices; sandbox quotes are not used here.

If you already have eligible, no-additional-fee production access:

- Enable API access in Namecheap **Profile > Tools > Business & Dev Tools**.
- Whitelist your public **IPv4** address.
- Set these environment variables in the same terminal before starting Streamlit:

  ```powershell
  $env:NAMECHEAP_API_USER = '<account username>'
  $env:NAMECHEAP_API_KEY = '<production API key>'
  $env:NAMECHEAP_CLIENT_IP = '<whitelisted public IPv4>'
  $env:NAMECHEAP_FREE_API_CONFIRMED = '1'
  .\.venv\Scripts\python.exe -m streamlit run app.py
  ```

The confirmation variable is an explicit assertion that you already have free
lookup access; without it the app stops before making a request. Access/payment
errors stop the operation. Keys and the client IP are never saved in projects.
If access would require spending money, leave live pricing disabled and use
actual observed registrar listings instead.

### Premium and registered resale listings

Live **registration premium** pricing is supported where Namecheap returns a
reliable quote. This adapter does **not** assert that `PremiumRegistrationPrice`
on an unavailable/registered domain is a resale asking price. Its read-only
API does not establish a reliable aftermarket quote for those domains, so they
show **Registered - resale unknown**.
GoDaddy is now the active provider and can return marketplace inventory with
separate premium acquisition fees; those fees are included in the displayed total.

To record a real resale listing, choose **Listing candidate**, select
**Registered**, set premium status, and enter the actual acquisition asking
price and optional renewal, in USD without currency signs/commas. Supply the
registrar URL or listing reference and confirm you checked the current listing,
including mandatory listed acquisition fees, then **Save observed listing**.
Leave unavailable prices blank. This works without an API account.
The source is explicitly labeled **User-observed registrar listing**, not API-verified.
There are no hardcoded example-domain prices or estimated prices.

The budget screens the one-year acquisition quote (or resale asking price),
including mandatory fees returned by the provider. Taxes may depend on your
billing address and are not returned here. Under-budget quotes without a final
all-in verification remain provisional and are excluded from Affordable Domains:
**never complete a manual purchase if the final all-in total exceeds $100**.
Nothing in this application buys, reserves or transfers a domain.

### API investigation sources (checked November 13, 2026)

- [Namecheap API introduction, authentication and IPv4 whitelist](https://www.namecheap.com/support/api/intro/)
- [Namecheap API FAQ: eligibility, fees and rate limits](https://www.namecheap.com/support/knowledgebase/article.aspx/9739/63/api-faq/)
- [Namecheap domains.check: premium fields and 50-domain batch limit](https://www.namecheap.com/support/api/methods/domains/check/)
- [Namecheap users.getPricing: standard tariffs and caching](https://www.namecheap.com/support/api/methods/users/get-pricing/)
- [GoDaddy current v3 access and pricing](https://developer.godaddy.com/en/docs/api-users/domains/pricing)
- [GoDaddy authentication/quickstart](https://developer.godaddy.com/en/docs/api-users/quickstart)
- [GoDaddy rate limits](https://developer.godaddy.com/en/docs/api-users/rate-limits)

GoDaddy is now integrated: current v3 documentation describes account/PAT
authentication, no membership/API access fee, no volume thresholds, and a billing
method for registration. Availability prices are indicative; quotes lock registration
prices, with premium/aftermarket fees separate. ICANN fees and taxes are not itemized
in v3 pricing, so an availability response alone cannot establish our firm all-in budget.
Current documented rate limits are per credential (600 per approximately 23 minutes,
subject to change; use response headers). Older v1/v2 account restrictions and price
units should not be assumed to describe current v3 access.
- [GoDaddy v3 OpenAPI, including 1-25 domain batch lookup and premium fees](https://developer.godaddy.com/openapi/domains-v3.json)

### Separate GoDaddy v3 read-only probe

`godaddy_pricing_test.py` remains a small opt-in CLI test. The active Streamlit
adapter reuses its token loader and parser, but the CLI itself never reads/writes saved projects.
Without `--live` it sends no requests and does not read a token.
With `--live`, it sends only two or three sequential HTTPS **GET** requests to
`https://api.godaddy.com/v3/domains/check-availability?domain=...`.
No quote, registration, transfer, modification or purchase endpoint is called.
It stops on the first HTTP/network/domain error without retries or redirects.

Run from your own local PowerShell terminal, **not Copilot chat**.
The simplest secure entry uses a hidden local prompt, with no token in command history:

```powershell
.\.venv\Scripts\python.exe godaddy_pricing_test.py --live --prompt-token
```

Alternatively set a process-only environment variable using hidden input:

```powershell
$securePat = Read-Host 'GoDaddy PAT (local terminal only)' -AsSecureString
$env:GODADDY_PAT = [System.Net.NetworkCredential]::new('', $securePat).Password
try {
    .\.venv\Scripts\python.exe godaddy_pricing_test.py --live
} finally {
    Remove-Item Env:\GODADDY_PAT -ErrorAction SilentlyContinue
    Remove-Variable securePat
}
```

Do not print the variable, use shell tracing/HTTP debug logging, paste the token
in chat, or put it on the command line. The API must receive the credential in
an HTTPS Authorization header, but the probe prints only normalized pricing
fields and fixed error messages, never headers, raw bodies, exceptions or tokens.

A file option is available if you want Copilot to run the probe: **create/edit
`.env.godaddy` locally yourself** next to the script and enter exactly one line
`GODADDY_PAT=<your token>`. Optional single/double quotes are supported.
This dedicated file and `.env` are excluded by `.gitignore`; a generic `.env`
is not loaded. Do not attach the file or ask Copilot to inspect its contents.
It is plaintext on disk, so restrict filesystem access and remove it after the
test if not needed. Do not share the project folder/ZIP with this file included.
An existing process `GODADDY_PAT` takes precedence over the dedicated file.

Default test names are fictional examples, not verified listings. To use two
or three other candidate names:

```powershell
.\.venv\Scripts\python.exe godaddy_pricing_test.py --live firstcandidate.com secondcandidate.com
```

The probe reports one-year registration and renewal prices when returned,
currency, inventory, premium purchase fees, and the sum of registration price
plus returned premium fees. **Premium fees are additional to the registration
price**; omitting them could make an expensive aftermarket listing look cheap.
`REGISTRY_PREMIUM` and `PREMIUM` identify registry-premium and marketplace
inventory; `REGISTRY` identifies standard inventory. Missing premium/renewal
fields remain unknown, and multi-year prices are not mislabeled as annual.
USD integers are cents: `919` means `$9.19`, not `$919` or micro-units.
Other supported currencies use their ISO minor-unit scale (JPY zero, KWD three);
unknown scales retain currency and raw minor units without guessing a decimal price.
Marketplace availability is not standard registration availability.

GoDaddy's [current search guide](https://developer.godaddy.com/en/docs/api-users/domains/search)
documents free read-only search without a payment method and requires
`domains.domain:read` / Domain Search access. A UI permission labeled
`domains.domain` must be confirmed by the actual response; its label alone does
not prove the token has the required access. HTTP 401 indicates an invalid/expired
token, 403 a scope/account restriction, 402 payment/access requirements, and 429
rate limiting. No error is retried automatically. Renew the token locally before
its expiry; credentials are never persisted to project files.

Check the provider's current access terms and possible charges before any live
request. Availability prices exclude ICANN fees and tax, so the probe deliberately sets
`budget_verified=false` and cannot make a candidate Affordable.
The Streamlit integration now provides persistent caching, shortlist-only planning,
rate-limit handling, separate registry/marketplace pricing, currency guards and
conservative budget classification while preserving Namecheap in code. The current
OpenAPI documents a read-only batch availability operation for 1-25 domains,
so 51 use three batches in Streamlit; this standalone probe
uses GET only. Missing or incomplete acquisition/renewal prices must remain
unknown rather than substituting standard tariffs or estimates.

## iPad and deployment

Streamlit can be used in Safari when hosted at an HTTPS address. No deployment is included or authorized in this ZIP. The app currently uses local project files and needs persistent storage or a backup/restore workflow before cloud deployment. Do not expose API keys or a publicly writable instance.

## Verification

Run `.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'` after installing requirements. See `AUTO_DECISIONS_FIX.md` for decision persistence details.

Pricing tests use temporary projects and mocked Namecheap XML and GoDaddy JSON/HTTP responses.
External HTTP/TCP connections are blocked in those tests (Windows asyncio's
local loopback socket pair is allowed). They cover budget boundaries, missing
prices, premium fees, resale, account/access errors, timeouts, rate limits,
batching, saved decisions, restart persistence, filters, target overrides and reruns.
GoDaddy UI tests screen exactly 59 of 150 candidates in 25/25/9 batches, preserve
manual decisions/research, test error/progress/caching behavior, and verify no token
appears in saved data or exports. Tests use isolated synthetic projects and do
not open or rewrite real project databases.
