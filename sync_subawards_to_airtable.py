#!/usr/bin/env python3
"""Sync USAspending subaward records to Airtable.

USAspending's /api/v2/subawards/ endpoint only returns subawards for ONE
specific parent award at a time (it does not support a broad recipient-name
or time-period search). It also only returns 6 fixed fields: id,
subaward_number, description, action_date, amount, recipient_name.

Also: /api/v2/search/spending_by_award/ requires award_type_codes to come
from a single type-group per request (contracts, grants, loans, idvs,
direct_payments, other_financial_assistance cannot be mixed in one call).

So this script works in three steps:
  1. Find the entity's prime awards via /api/v2/search/spending_by_award/,
     querying the 'contracts' group and the 'grants' group separately
     (the two groups relevant to this project) and merging results.
  2. For each prime award's generated_internal_id, call /api/v2/subawards/
     to list its subawards.
  3. Normalize and upsert into Airtable.

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
  SYNC_MAX_PRIME_AWARDS (default: 50) -- how many prime awards to scan for subawards
  SYNC_MAX_ROWS (default: 250) -- max subaward rows to write
"""

import hashlib
import json
import os
import sys
import time
from datetime import date

import requests


USASPENDING_BASE = "https://api.usaspending.gov/api/v2"
SPENDING_BY_AWARD_URL = f"{USASPENDING_BASE}/search/spending_by_award/"
SUBAWARDS_URL = f"{USASPENDING_BASE}/subawards/"
AIRTABLE_API = "https://api.airtable.com/v0"

# USAspending requires award_type_codes to come from exactly one of these groups per request.
AWARD_TYPE_GROUPS = {
    "contracts": ["A", "B", "C", "D"],
    "grants": ["02", "03", "04", "05"],
}


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
    safe_id = entity_id.replace("'", "\\'")
    formula = "{{Entity_ID}}='{}'".format(safe_id)
    url = f"{AIRTABLE_API}/{base_id}/{entity_table}"
    resp = requests.get(url, headers=headers, params={"filterByFormula": formula, "pageSize": 1}, timeout=60)
    resp.raise_for_status()
    records = resp.json().get("records", [])
    if not records:
        raise RuntimeError(f"No Entity record found for Entity_ID={entity_id}")
    return records[0]["id"]


def fetch_prime_awards_for_group(recipient_search, fy_start, fy_end, type_codes, max_awards):
    payload = {
        "filters": {
            "time_period": [{"start_date": f"{fy_start}-10-01", "end_date": f"{fy_end}-09-30"}],
            "recipient_search_text": [recipient_search],
            "award_type_codes": type_codes,
        },
        "fields": ["Award ID", "Recipient Name", "Award Amount"],
        "sort": "Award Amount",
        "order": "desc",
        "page": 1,
        "limit": min(max_awards, 100),
    }
    resp = requests.post(SPENDING_BY_AWARD_URL, json=payload, timeout=90)
    if resp.status_code == 400:
        return [], payload
    resp.raise_for_status()
    body = resp.json()
    results = body.get("results", [])
    awards = []
    for row in results[:max_awards]:
        gid = row.get("generated_internal_id") or row.get("internal_id")
        if gid:
            awards.append({"generated_internal_id": gid, "Award ID": row.get("Award ID"), "Recipient Name": row.get("Recipient Name")})
    return awards, payload


def fetch_prime_awards(recipient_search, fy_start, fy_end, max_awards):
    """Step 1: find the entity's prime awards, querying each award-type group separately
    (USAspending rejects requests that mix codes from different groups) and merging results."""
    all_awards = []
    seen_ids = set()
    queries_used = []
    for group_name, type_codes in AWARD_TYPE_GROUPS.items():
        try:
            group_awards, payload = fetch_prime_awards_for_group(recipient_search, fy_start, fy_end, type_codes, max_awards)
        except requests.HTTPError as exc:
            print(f"  Group '{group_name}' query failed: {exc}", file=sys.stderr)
            continue
        queries_used.append({"group": group_name, "payload": payload})
        for award in group_awards:
            if award["generated_internal_id"] not in seen_ids:
                seen_ids.add(award["generated_internal_id"])
                all_awards.append(award)
    return all_awards[:max_awards], queries_used


def fetch_subawards_for_award(award_id, max_rows):
    """Step 2: list subawards under one specific prime award."""
    payload = {
        "award_id": award_id,
        "sort": "action_date",
        "order": "desc",
        "page": 1,
        "limit": min(max_rows, 100),
    }
    results = []
    while len(results) < max_rows:
        resp = requests.post(SUBAWARDS_URL, json=payload, timeout=90)
        if resp.status_code == 400:
            # Some awards legitimately have zero subawards or an unsupported award type; skip rather than fail the whole run.
            return results
        resp.raise_for_status()
        body = resp.json()
        page_results = body.get("results", [])
        if not page_results:
            break
        results.extend(page_results)
        page_metadata = body.get("page_metadata", {})
        if not page_metadata.get("hasNext", False):
            break
        payload["page"] = payload.get("page", 1) + 1
    return results[:max_rows]


def normalize(row, entity_id, entity_record_id, prime_award, batch_id):
    subaward_number = str(row.get("subaward_number") or row.get("id") or "")
    amount = row.get("amount")
    action_date = row.get("action_date")
    recipient_name = row.get("recipient_name")
    description = row.get("description")
    prime_award_id = prime_award.get("Award ID")
    prime_recipient = prime_award.get("Recipient Name")

    stable_key = "|".join([entity_id, subaward_number, str(prime_award_id or ""), str(recipient_name or ""), str(action_date or ""), str(amount or "")])
    internal_id = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:24]
    raw_hash = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    normalized = {
        "generated_internal_id": internal_id,
        "Entity_ID": entity_id,
        "Entity": [entity_record_id],
        "subaward_id": subaward_number,
        "prime_award_id": str(prime_award_id or ""),
        "subawardee_name": recipient_name,
        "subawardee_UEI": None,
        "subaward_amount": amount,
        "action_date": action_date,
        "prime_award_recipient": prime_recipient,
        "prime_award_recipient_UEI": None,
        "awarding_agency": None,
        "funding_agency": None,
        "description": description,
        "place_of_performance": None,
        "source_batch_id": batch_id,
        "review_flag": "OK" if subaward_number else "Needs review",
    }

    raw = {
        "raw_hash": raw_hash,
        "generated_internal_id": internal_id,
        "import_batch_id": batch_id,
        "pull_date": date.today().isoformat(),
        "query_scope": json.dumps({"entity_id": entity_id, "prime_award_id": prime_award_id}),
        "source_url": SUBAWARDS_URL,
        "subaward_id_raw": subaward_number,
        "prime_award_id_raw": str(prime_award_id or ""),
        "subawardee_name_raw": recipient_name,
        "subawardee_uei_raw": None,
        "subaward_amount_raw": amount,
        "action_date_raw": action_date,
        "raw_json": json.dumps(row, default=str),
        "Normalization status": "Synced" if subaward_number else "Needs review",
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
    max_prime_awards = int(optional("SYNC_MAX_PRIME_AWARDS", "50"))
    max_rows = int(optional("SYNC_MAX_ROWS", "250"))

    headers = airtable_headers(token)
    entity_record_id = get_entity_record_id(base_id, entity_table, headers, entity_id)
    batch_id = f"SUBAWARDS-{entity_id}-{date.today().isoformat()}"

    print(f"Step 1: finding prime awards for {entity_name} ({recipient_search}) FY{fy_start}-FY{fy_end}...")
    prime_awards, queries_used = fetch_prime_awards(recipient_search, fy_start, fy_end, max_prime_awards)
    print(f"Found {len(prime_awards)} prime awards to check for subawards.")

    normalized_rows = []
    raw_rows = []
    for prime_award in prime_awards:
        gid = prime_award["generated_internal_id"]
        try:
            subaward_rows = fetch_subawards_for_award(gid, max_rows)
        except requests.HTTPError as exc:
            print(f"  Skipping award {gid}: {exc}", file=sys.stderr)
            continue
        for row in subaward_rows:
            normalized, raw = normalize(row, entity_id, entity_record_id, prime_award, batch_id)
            normalized_rows.append(normalized)
            raw_rows.append(raw)
        if subaward_rows:
            print(f"  Award {prime_award.get('Award ID')}: {len(subaward_rows)} subawards")
        time.sleep(0.2)
        if len(normalized_rows) >= max_rows:
            break

    normalized_rows = normalized_rows[:max_rows]
    raw_rows = raw_rows[:max_rows]
    print(f"Total subaward rows collected: {len(normalized_rows)}")

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
                "prime_awards_checked": len(prime_awards),
                "subawards_retrieved": len(normalized_rows),
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
        print(f"HTTP error: {exc}\n{detail}", file=sys.stderr)
        raise
    except Exception as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        raise
