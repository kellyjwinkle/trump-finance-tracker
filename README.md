# Trump Finance Tracker

Research pipeline that syncs U.S. federal spending data (USAspending.gov) into an Airtable base, cross-referenced with documented ownership/investment relationships involving Trump-affiliated entities.

This project deliberately separates **confirmed government spending records** from **reported relationships/allegations**. Never merge these two categories when drawing conclusions.

## Airtable base

Base name: `Trump Finance Tracker`
Base ID: `appg15pT8O0Q3A1oo`

### Tables

| Table | Purpose |
|---|---|
| `Entity` | Resolved companies, funds, trusts, nonprofits, people. Holds official identifiers (UEI, CAGE, EIN, SEC CIK) and rollups of total obligations/award counts. |
| `Gov_Awards` | Normalized federal award/transaction records, one row per award. Linked to `Entity` and `Sources`. |
| `Import_Raw_USASpending` | Immutable audit trail of raw USAspending API responses, keyed by `raw_hash` for idempotent re-syncing. |
| `Sources` | Evidence registry: every citation (USAspending, SEC EDGAR, IRS, court dockets, investigative reporting) linked to the awards/relationships it supports. |
| `Relationships` | Documented links between entities: ownership, investment, board roles, family connections. Each has an `Evidence level` and `Claim status` so reported claims are never conflated with documented facts. |

## Sync script

`sync_usaspending_to_airtable.py` queries USAspending's `/api/v2/search/spending_by_award/` endpoint for a given recipient, normalizes the results, and upserts them into `Gov_Awards` and `Import_Raw_USASpending` via the Airtable Web API. It deduplicates using a SHA-256 hash of key award fields, so re-running it is safe and idempotent.

### Setup

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
# edit .env with your Airtable Personal Access Token
```

Create an Airtable Personal Access Token at https://airtable.com/create/tokens with scopes:

- `data.records:read`
- `data.records:write`
- `schema.bases:read`

Scope it to only the `Trump Finance Tracker` base.

### Run

```bash
python sync_usaspending_to_airtable.py
```

To sync a different entity, override the environment variables:

```bash
SYNC_ENTITY_ID=E007 SYNC_ENTITY_SEARCH="DOMINARI HOLDINGS INC" SYNC_ENTITY_NAME="Dominari Holdings Inc." python sync_usaspending_to_airtable.py
```

### Automated schedule

`.github/workflows/usaspending_sync.yml` runs the sync every Monday at 7:15 AM Eastern (11:15 UTC) via GitHub Actions. You can also trigger it manually from the Actions tab ("Run workflow").

Add these repository secrets under **Settings → Secrets and variables → Actions**:

- `AIRTABLE_PAT`
- `AIRTABLE_BASE_ID`

## Data integrity rules

- Never sum `award_ceiling`, `transaction_amount`, and `total_obligation` together — they measure different things (ceiling vs. actual obligation vs. transaction-level amount).
- Preserve negative transaction amounts — they represent deobligations/corrections, not errors.
- Every relationship claim in the `Relationships` table must have a linked `Sources` record and an honest `Evidence level` (official filing vs. investigative reporting vs. allegation).
- Manually seeded rows (tagged `MANUAL-SEED-*` in `source_batch_id`) should be replaced by real synced data and then deleted once verified.

## Current test entity

- **Vulcan Elements Inc.** (`E008`) — rare-earth magnet manufacturer, reported recipient of a ~$620M DoD Office of Strategic Capital conditional loan commitment, with 1789 Capital (Donald Trump Jr.-linked fund) as a reported investor.
