from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv


load_dotenv()

AIRTABLE_PAT = os.environ["AIRTABLE_PAT"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]

ENTITY_TABLE = os.getenv("AIRTABLE_ENTITY_TABLE", "Entity")
AWARDS_TABLE = os.getenv("AIRTABLE_AWARDS_TABLE", "Gov_Awards")
RAW_TABLE = os.getenv("AIRTABLE_RAW_TABLE", "Import_Raw_USASpending")

USA_BASE = "https://api.usaspending.gov/api/v2"
AIRTABLE_BASE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"

ENTITY_ID = os.getenv("SYNC_ENTITY_ID", "E008")
ENTITY_SEARCH = os.getenv("SYNC_ENTITY_SEARCH", "VULCAN ELEMENTS INC")
ENTITY_CANONICAL_NAME = os.getenv("SYNC_ENTITY_NAME", "Vulcan Elements Inc.")

FY_START = int(os.getenv("SYNC_FY_START", "2020"))
FY_END = int(os.getenv("SYNC_FY_END", "2026"))
MAX_AWARD_ROWS = int(os.getenv("SYNC_MAX_ROWS", "250"))
AWARD_GROUPING = os.getenv("SYNC_AWARD_GROUPING", "all")
AIRTABLE_BATCH_SIZE = 10
REQUEST_DELAY_SECONDS = 0.25

# USAspending award_type_codes reference:
# Contracts: A, B, C, D
# IDVs: IDV_A, IDV_B, IDV_B_A, IDV_B_B, IDV_B_C, IDV_C, IDV_D, IDV_E
# Grants/assistance: 02, 03, 04, 05
# Loans: 07, 08
# Direct payments: 06, 10, 09, 11
CONTRACT_CODES = ["A", "B", "C", "D"]
IDV_CODES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"]
ASSISTANCE_CODES = ["02", "03", "04", "05", "06", "07", "08", "09", "10", "11"]
ALL_AWARD_TYPE_CODES = CONTRACT_CODES + IDV_CODES + ASSISTANCE_CODES

AWARD_TYPE_CODES_BY_GROUPING = {
    "all": ALL_AWARD_TYPE_CODES,
    "contract": CONTRACT_CODES,
    "assistance": ASSISTANCE_CODES,
}

# Fields valid on the /search/spending_by_award/ endpoint (award-level search).
# NOTE: "Action Date" and "Federal Action Obligation" are transaction-search fields
# and are NOT valid here — using them causes a 422. Parent Recipient Name/UEI and
# NAICS/PSC description fields are also not part of this endpoint's schema.
AWARD_SEARCH_FIELDS = [
    "Award ID",
    "generated_internal_id",
    "Recipient Name",
    "Recipient UEI",
    "Awarding Agency",
    "Funding Agency",
    "Description",
    "Place of Performance City Code",
    "Place of Performance State Code",
    "Place of Performance Country Code",
    "Start Date",
    "End Date",
    "Award Amount",
    "Total Outlays",
    "Base Obligation Date",
    "Last Modified Date",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


class AirtableClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    def _url(self, table_name: str) -> str:
        return f"{self.base_url}/{quote(table_name, safe='')}"

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        payload: dict | None = None,
        retries: int = 5,
    ) -> dict[str, Any]:
        for attempt in range(retries):
            response = self.session.request(
                method,
                url,
                params=params,
                json=payload,
                timeout=45,
            )

            if response.status_code == 429:
                wait_seconds = 30 if attempt == 0 else 30 * (attempt + 1)
                logging.warning(
                    "Airtable rate limit reached. Waiting %s seconds.",
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            if 500 <= response.status_code < 600:
                wait_seconds = 2**attempt
                logging.warning(
                    "Airtable server error %s. Retrying in %s seconds.",
                    response.status_code,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            response.raise_for_status()
            time.sleep(REQUEST_DELAY_SECONDS)
            return response.json()

        raise RuntimeError(f"Airtable request failed after {retries} attempts: {url}")

    def list_records(
        self,
        table_name: str,
        *,
        fields: list[str] | None = None,
        filter_formula: str | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset = None

        while True:
            params: dict[str, Any] = {"pageSize": 100}

            if fields:
                for index, field_name in enumerate(fields):
                    params[f"fields[{index}]"] = field_name

            if filter_formula:
                params["filterByFormula"] = filter_formula

            if offset:
                params["offset"] = offset

            data = self.request("GET", self._url(table_name), params=params)
            records.extend(data.get("records", []))
            offset = data.get("offset")

            if not offset:
                break

        return records

    def create_records(
        self,
        table_name: str,
        records: list[dict[str, Any]],
    ) -> None:
        for batch in chunked(records, AIRTABLE_BATCH_SIZE):
            self.request(
                "POST",
                self._url(table_name),
                payload={"records": batch, "typecast": True},
            )

    def update_records(
        self,
        table_name: str,
        records: list[dict[str, Any]],
    ) -> None:
        for batch in chunked(records, AIRTABLE_BATCH_SIZE):
            self.request(
                "PATCH",
                self._url(table_name),
                payload={"records": batch, "typecast": True},
            )

    def update_record(
        self,
        table_name: str,
        record_id: str,
        fields: dict[str, Any],
    ) -> None:
        self.request(
            "PATCH",
            f"{self._url(table_name)}/{record_id}",
            payload={"fields": fields, "typecast": True},
        )

    def create_record(
        self,
        table_name: str,
        fields: dict[str, Any],
    ) -> str:
        data = self.request(
            "POST",
            self._url(table_name),
            payload={"fields": fields, "typecast": True},
        )
        return data["id"]


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def as_number(value: Any) -> float | None:
    if value in (None, ""):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_date(value: Any) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def field_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def make_raw_hash(row: dict[str, Any]) -> str:
    key_parts = [
        as_text(field_value(row, "generated_internal_id")),
        as_text(field_value(row, "Award ID", "award_id")),
        as_text(field_value(row, "Recipient Name", "recipient_name")),
        as_text(field_value(row, "Base Obligation Date", "base_obligation_date")),
        as_text(field_value(row, "Award Amount", "award_amount")),
    ]

    raw_key = "||".join(key_parts)
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def usaspending_post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        f"{USA_BASE}{endpoint}",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=60,
    )

    if response.status_code == 422:
        logging.error("USAspending 422 response body: %s", response.text)

    response.raise_for_status()
    return response.json()


def build_time_periods(start_fy: int, end_fy: int) -> list[dict[str, str]]:
    return [
        {
            "start_date": f"{fiscal_year - 1}-10-01",
            "end_date": f"{fiscal_year}-09-30",
        }
        for fiscal_year in range(start_fy, end_fy + 1)
    ]


def build_filters(recipient_search_text: str) -> dict[str, Any]:
    award_type_codes = AWARD_TYPE_CODES_BY_GROUPING.get(
        AWARD_GROUPING, ALL_AWARD_TYPE_CODES
    )

    return {
        "recipient_search_text": [recipient_search_text],
        "time_period": build_time_periods(FY_START, FY_END),
        "award_type_codes": award_type_codes,
    }


def search_usaspending_awards(
    recipient_search_text: str,
    max_rows: int,
) -> list[dict[str, Any]]:
    page = 1
    rows: list[dict[str, Any]] = []

    while len(rows) < max_rows:
        payload = {
            "filters": build_filters(recipient_search_text),
            "fields": AWARD_SEARCH_FIELDS,
            "page": page,
            "limit": 100,
            "sort": "Award Amount",
            "order": "desc",
            "subawards": False,
        }

        data = usaspending_post("/search/spending_by_award/", payload)
        page_rows = data.get("results", [])

        if not page_rows:
            break

        rows.extend(page_rows)

        metadata = data.get("page_metadata", {})
        if not metadata.get("hasNext"):
            break

        page += 1

    return rows[:max_rows]


def ensure_entity(client: AirtableClient) -> str:
    existing_records = client.list_records(
        ENTITY_TABLE,
        fields=["Entity_ID", "Legal / known name"],
        filter_formula=f"{{Entity_ID}}='{ENTITY_ID}'",
    )

    entity_fields = {
        "Entity_ID": ENTITY_ID,
        "Legal / known name": ENTITY_CANONICAL_NAME,
        "Entity type": "Private company",
        "Verification status": "USAspending match",
        "Notes": (
            f"Seeded/synced from USAspending using recipient search: {ENTITY_SEARCH}"
        ),
    }

    if existing_records:
        record_id = existing_records[0]["id"]
        client.update_record(ENTITY_TABLE, record_id, entity_fields)
        logging.info("Updated Entity record %s", record_id)
        return record_id

    record_id = client.create_record(ENTITY_TABLE, entity_fields)
    logging.info("Created Entity record %s", record_id)
    return record_id


def load_existing_indexes(
    client: AirtableClient,
) -> tuple[dict[str, str], dict[str, str]]:
    award_records = client.list_records(
        AWARDS_TABLE,
        fields=[
            "generated_internal_id",
            "award_id",
            "action_date",
            "transaction_amount",
        ],
    )

    raw_records = client.list_records(
        RAW_TABLE,
        fields=["raw_hash"],
    )

    awards_by_key: dict[str, str] = {}
    for record in award_records:
        fields = record.get("fields", {})
        key = "||".join(
            [
                as_text(fields.get("generated_internal_id")),
                as_text(fields.get("award_id")),
                as_text(fields.get("action_date")),
                as_text(fields.get("transaction_amount")),
            ]
        )
        awards_by_key[key] = record["id"]

    raws_by_hash: dict[str, str] = {}
    for record in raw_records:
        raw_hash = as_text(record.get("fields", {}).get("raw_hash"))
        if raw_hash:
            raws_by_hash[raw_hash] = record["id"]

    return awards_by_key, raws_by_hash


def map_raw_record(
    row: dict[str, Any],
    import_batch_id: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    raw_hash = make_raw_hash(row)
    base_obligation_date = as_date(field_value(row, "Base Obligation Date"))

    return {
        "import_batch_id": import_batch_id,
        "pull_date": now,
        "query_scope": (
            f"Recipient search: {ENTITY_SEARCH}; "
            f"FY{FY_START}-FY{FY_END}; grouping={AWARD_GROUPING}"
        ),
        "source_url": "https://api.usaspending.gov/api/v2/search/spending_by_award/",
        "generated_internal_id": as_text(field_value(row, "generated_internal_id")),
        "award_id_raw": as_text(field_value(row, "Award ID", "award_id")),
        "recipient_name_raw": as_text(
            field_value(row, "Recipient Name", "recipient_name")
        ),
        "recipient_uei_raw": as_text(field_value(row, "Recipient UEI")),
        "awarding_agency_raw": as_text(field_value(row, "Awarding Agency")),
        "funding_agency_raw": as_text(field_value(row, "Funding Agency")),
        "action_date_raw": base_obligation_date,
        "start_date_raw": as_date(field_value(row, "Start Date")),
        "end_date_raw": as_date(field_value(row, "End Date")),
        "award_amount_raw": as_number(field_value(row, "Award Amount")),
        "obligated_amount_raw": as_number(field_value(row, "Total Outlays")),
        "description_raw": as_text(field_value(row, "Description")),
        "place_of_performance_raw": " / ".join(
            value
            for value in [
                as_text(field_value(row, "Place of Performance City Code")),
                as_text(field_value(row, "Place of Performance State Code")),
                as_text(field_value(row, "Place of Performance Country Code")),
            ]
            if value
        ),
        "raw_hash": raw_hash,
        "Normalization status": "Synced",
    }


def map_award_record(
    row: dict[str, Any],
    import_batch_id: str,
    entity_airtable_record_id: str,
) -> dict[str, Any]:
    award_id = as_text(field_value(row, "Award ID", "award_id"))
    award_amount = as_number(field_value(row, "Award Amount"))
    total_outlays = as_number(field_value(row, "Total Outlays"))
    base_obligation_date = as_date(field_value(row, "Base Obligation Date"))

    return {
        "Entity_ID": ENTITY_ID,
        "Entity": [entity_airtable_record_id],
        "generated_internal_id": as_text(field_value(row, "generated_internal_id")),
        "award_id": award_id,
        "award_id_type": "Other",
        "award_type": AWARD_GROUPING,
        "awarding_agency": as_text(field_value(row, "Awarding Agency")),
        "funding_agency": as_text(field_value(row, "Funding Agency")),
        "recipient_name_reported": as_text(
            field_value(row, "Recipient Name", "recipient_name")
        ),
        "recipient_UEI": as_text(field_value(row, "Recipient UEI")),
        "action_date": base_obligation_date,
        "period_start": as_date(field_value(row, "Start Date")),
        "period_end": as_date(field_value(row, "End Date")),
        "transaction_amount": award_amount,
        "total_obligation": total_outlays if total_outlays is not None else award_amount,
        "award_ceiling": award_amount,
        "description": as_text(field_value(row, "Description")),
        "place_of_performance": " / ".join(
            value
            for value in [
                as_text(field_value(row, "Place of Performance City Code")),
                as_text(field_value(row, "Place of Performance State Code")),
                as_text(field_value(row, "Place of Performance Country Code")),
            ]
            if value
        ),
        "source_batch_id": import_batch_id,
        "review_flag": (
            "OK"
            if as_text(field_value(row, "Recipient UEI"))
            else "Needs review"
        ),
    }


def sync() -> None:
    batch_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    import_batch_id = f"USAS-{batch_stamp}-{ENTITY_ID}-01"

    client = AirtableClient(AIRTABLE_BASE_URL, AIRTABLE_PAT)

    entity_airtable_record_id = ensure_entity(client)

    rows = search_usaspending_awards(
        recipient_search_text=ENTITY_SEARCH,
        max_rows=MAX_AWARD_ROWS,
    )

    logging.info("Fetched %s USAspending award rows.", len(rows))

    awards_by_key, raws_by_hash = load_existing_indexes(client)

    raw_creates = []
    raw_updates = []
    award_creates = []
    award_updates = []

    for row in rows:
        raw_fields = map_raw_record(row, import_batch_id)
        raw_hash = raw_fields["raw_hash"]

        if raw_hash in raws_by_hash:
            raw_updates.append(
                {
                    "id": raws_by_hash[raw_hash],
                    "fields": raw_fields,
                }
            )
        else:
            raw_creates.append({"fields": raw_fields})

        award_fields = map_award_record(
            row,
            import_batch_id,
            entity_airtable_record_id,
        )

        award_key = "||".join(
            [
                as_text(award_fields["generated_internal_id"]),
                as_text(award_fields["award_id"]),
                as_text(award_fields["action_date"]),
                as_text(award_fields["transaction_amount"]),
            ]
        )

        if award_key in awards_by_key:
            award_updates.append(
                {
                    "id": awards_by_key[award_key],
                    "fields": award_fields,
                }
            )
        else:
            award_creates.append({"fields": award_fields})

    logging.info(
        "Raw: %s creates, %s updates | Awards: %s creates, %s updates",
        len(raw_creates),
        len(raw_updates),
        len(award_creates),
        len(award_updates),
    )

    client.create_records(RAW_TABLE, raw_creates)
    client.update_records(RAW_TABLE, raw_updates)
    client.create_records(AWARDS_TABLE, award_creates)
    client.update_records(AWARDS_TABLE, award_updates)

    logging.info("Sync complete. Batch ID: %s", import_batch_id)


if __name__ == "__main__":
    sync()
