#!/usr/bin/env python3
"""Sync USAspending subaward records to Airtable.

Required environment variables:
  AIRTABLE_PAT
  AIRTABLE_BASE_ID
  SYNC_ENTITY_ID
  SYNC_ENTITY_SEARCH
  SYNC_ENTITY_NAME

Optional environment variables:
  AIRTABLE_ENTITY_TABLE (default: Entity)
  AIRTABLE_SUBAWARDS_TABLE (default: Subawards)
  AIRTABLE_RAW_SUBAWARDS_TABLE (default: Import_Raw_Subawards)
  SYNC_FY_START (default: 2020)
  SYNC_FY_END (default: current fiscal year)
  SYNC_MAX_ROWS (default: 250)
"""

import hashlib
import json
import os
import sys
from datetime import date

import requests


USASPENDING_URL = "https://api.usaspending.gov/api/v2/subawards/"
AIRTABLE_API = "https://api.airtable.com/v0"


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def optional(name, default=""):
    return os.environ.get(name, default).strip() or default


def chunked(items, size=10):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fiscal_year_end():
    today = date.today()
    return today.year + 1 if today.month >= 10 else today.year


def airtable_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def airtable_list_all(base_id, table_name, headers, fields=None):
    url = f"{AIRTABLE_API}/{base_id}/{table_name}"
    params = {"pageSize": 100}
    if fields:
        params["fields[]"] = fields
    rows = []
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        body = resp.json()
        rows.extend(body.get("records", []))
        offset = body.get("offset")
        if not offset:
            return rows
        params["offset"] = offset


def airtable_create(base_id, table_name, headers, records):
    url = f"{AIRTABLE_API}/{base_id}/{table_name}"
    for batch in chunked(records):
        resp = requests.post(url, headers=headers, json={"records": batch, "typecast": True}, timeout=60)
        resp.raise_for_status()


def airtable_update(base_id, table_name, headers, records):
    url = f"{AIRTABLE_API}/{base_id}/{table_name}"
    for batch in chunked(records):
        resp = requests.patch(url, headers=headers, json={"records": batch, "typecast": True}, timeout=60)
        resp.raise_for_status()


def get_entity_record_id(base_id, entity_table, headers, entity_id):
    formula = "{Entity_ID}='{}'".format(entity_id.replace("'", "\\'"))
    url = f"{AIRTABLE_API}/{base_id}/{entity_table}"
    resp = requests.get(url, headers=headers, params={"filterByFormula": formula, "pageSize": 1}, timeout=60)
    resp.raise_for_status()
    records = resp.json().get("records", [])
    if not records:
        raise RuntimeError(f"No Entity record found for Entity_ID={entity_id}")
    return records[0]["id"]


def fetch_subawards(recipient_search, fy_start, fy_end, max_rows):
    filters = {
        "time_period": [{"start_date": f"{fy_start}-10-01", "end_date": f"{fy_end}-09-30"}],
        "recipient_search_text": [recipient_search],
        "award_type_codes": [
            "A", "B", "C", "D",  # contracts
            "02", "03", "04", "05", # grants/cooperative agreements
            "07", "08", "09", "10", "11", # direct payments/loans
        ],
    }
    payload = {
        "filters": filters,
        "fields": [
            "Sub-Award ID",
            "Sub-Awardee Name",
            "Sub-Awardee UEI",
            "Sub-Award Amount",
            "Sub-Award Date",
            "Award ID",
            "Awarding Agency",
            "Funding Agency",
            "Prime Award Recipient Name",
            "Prime Award Recipient UEI",
            "Award Description",
            "Place of Performance",
        ],
        "page": 1,
        "limit": min(max_rows, 100),
        "sort": "Sub-Award Date",
        "order": "desc",
    }

    results = []
    while len(results) < max_rows:
        resp = requests.post(USASPENDING_URL, json=payload, timeout=90)
        resp.raise_for_status()
        body = resp.json()
        page_results = body.get("results", [])
        if not page_results:
            break
        results.extend(page_results)
        if not body.get("page_metadata", {}).get("hasNext", False):
            break
        payload["page"] += 1

    return results[:max_rows], payload


def pick(row, *names):
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def normalize(row, entity_id, entity_record_id, batch_id):
    subaward_id = str(pick(row, "Sub-Award ID", "subaward_id", "subaward_number") or "")
    award_id = str(pick(row, "Award ID", "award_id", "prime_award_id") or "")
    subawardee = pick(row, "Sub-Awardee Name", "subawardee_name", "sub_awardee_name")
    amount = pick(row, "Sub-Award Amount", "subaward_amount", "sub_award_amount")
    action_date = pick(row, "Sub-Award Date", "subaward_date", "sub_award_date")

    stable_key = "|".join([entity_id, subaward_id, award_id, str(subawardee or ""), str(action_date or ""), str(amount or "")])
    internal_id = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:24]
    raw_hash = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    normalized = {
        "generated_internal_id": internal_id,
        "Entity_ID": entity_id,
        "Entity": [entity_record_id],
        "subaward_id": subaward_id,
        "prime_award_id": award_id,
        "subawardee_name": subawardee,
        "subawardee_UEI": pick(row, "Sub-Awardee UEI", "subawardee_uei", "sub_awardee_uei"),
        "subaward_amount": amount,
        "action_date": action_date,
        "prime_award_recipient": pick(row, "Prime Award Recipient Name", "prime_award_recipient_name"),
        "prime_award_recipient_UEI": pick(row, "Prime Award Recipient UEI", "prime_award_recipient_uei"),
        "awarding_agency": pick(row, "Awarding Agency", "awarding_agency"),
        "funding_agency": pick(row, "Funding Agency", "funding_agency"),
        "description": pick(row, "Award Description", "award_description", "description"),
        "place_of_performance": pick(row, "Place of Performance", "place_of_performance"),
        "source_batch_id": batch_id,
        "review_flag": "OK" if subaward_id else "Needs review",
    }

    raw = {
        "raw_hash": raw_hash,
        "generated_internal_id": internal_id,
        "import_batch_id": batch_id,
        "pull_date": date.today().isoformat(),
        "query_scope": json.dumps({"entity_id": entity_id, "recipient_search": optional("SYNC_ENTITY_SEARCH")}),
        "source_url": "https://api.usaspending.gov/api/v2/subawards/",
        "subaward_id_raw": subaward_id,
        "prime_award_id_raw": award_id,
        "subawardee_name_raw": subawardee,
        "subawardee_uei_raw": pick(row, "Sub-Awardee UEI", "subawardee_uei", "sub_awardee_uei"),
        "subaward_amount_raw": amount,
        "action_date_raw": action_date,
        "raw_json": json.dumps(row, default=str),
        "Normalization status": "Synced" if subaward_id else "Needs review",
    }
    return normalized, raw


def existing_by_key(base_id, table_name, headers, key_field="generated_internal_id"):
    records = airtable_list_all(base_id, table_name, headers)
    return {
        r.get("fields", {}).get(key_field): r["id"]
        for r in records
        if r.get("fields", {}).get(key_field)
    }


def main():
    token = required("AIRTABLE_PAT")
    base_id = required("AIRTABLE_BASE_ID")
    entity_id = required("SYNC_ENTITY_ID")
    recipient_search = required("SYNC_ENTITY_SEARCH")
    entity_name = required("SYNC_ENTITY_NAME")

    entity_table = optional("AIRTABLE_ENTITY_TABLE", "Entity")
    subawards_table = optional("AIRTABLE_SUBAWARDS_TABLE", "Subawards")
    raw_table = optional("AIRTABLE_RAW_SUBAWARDS_TABLE", "Import_Raw_Subawards")
    fy_start = int(optional("SYNC_FY_START", "2020"))
    fy_end = int(optional("SYNC_FY_END", str(fiscal_year_end())))
    max_rows = int(optional("SYNC_MAX_ROWS", "250"))

    headers = airtable_headers(token)
    entity_record_id = get_entity_record_id(base_id, entity_table, headers, entity_id)
    batch_id = f"SUBAWARDS-{entity_id}-{date.today().isoformat()}"

    print(f"Fetching subawards for {entity_name} ({recipient_search}) FY{fy_start}-FY{fy_end}...")
    rows, query_payload = fetch_subawards(recipient_search, fy_start, fy_end, max_rows)
    print(f"USAspending returned {len(rows)} subaward rows.")

    normalized_rows = []
    raw_rows = []
    for row in rows:
        normalized, raw = normalize(row, entity_id, entity_record_id, batch_id)
        normalized_rows.append(normalized)
        raw_rows.append(raw)

    existing_subawards = existing_by_key(base_id, subawards_table, headers)
    create_rows = []
    update_rows = []
    for fields in normalized_rows:
        existing_id = existing_subawards.get(fields["generated_internal_id"])
        if existing_id:
            update_rows.append({"id": existing_id, "fields": fields})
        else:
            create_rows.append({"fields": fields})

    if create_rows:
        airtable_create(base_id, subawards_table, headers, create_rows)
    if update_rows:
        airtable_update(base_id, subawards_table, headers, update_rows)

    existing_raw = existing_by_key(base_id, raw_table, headers, key_field="raw_hash")
    new_raw_rows = [{"fields": r} for r in raw_rows if r["raw_hash"] not in existing_raw]
    if new_raw_rows:
        airtable_create(base_id, raw_table, headers, new_raw_rows)

    print(
        json.dumps(
            {
                "entity": entity_name,
                "query": query_payload,
                "retrieved": len(rows),
                "subawards_created": len(create_rows),
                "subawards_updated": len(update_rows),
                "raw_created": len(new_raw_rows),
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else ""
        print(f"HTTP error: {exc}\\n{detail}", file=sys.stderr)
        raise
    except Exception as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        raise
