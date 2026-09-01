import os
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Subaward Flows", layout="wide")
st.title("Subaward Flows")
st.caption("Federal money flowing from tracked prime recipients to reported subawardees. Subaward data is sourced from USAspending.")


def get_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


AIRTABLE_PAT = get_secret("AIRTABLE_PAT")
AIRTABLE_BASE_ID = get_secret("AIRTABLE_BASE_ID")
ENTITY_TABLE = get_secret("AIRTABLE_ENTITY_TABLE") or "Entity"
SUBAWARDS_TABLE = get_secret("AIRTABLE_SUBAWARDS_TABLE") or "Subawards"

if not AIRTABLE_PAT or not AIRTABLE_BASE_ID:
    st.error("Missing AIRTABLE_PAT or AIRTABLE_BASE_ID in Streamlit secrets/environment.")
    st.stop()

HEADERS = {"Authorization": f"Bearer {AIRTABLE_PAT}"}
BASE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"


@st.cache_data(ttl=300)
def fetch_all_records(table_name):
    records = []
    params = {"pageSize": 100}
    url = f"{BASE_URL}/{table_name}"
    while True:
        response = requests.get(url, headers=HEADERS, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        records.extend(payload.get("records", []))
        offset = payload.get("offset")
        if not offset:
            break
        params["offset"] = offset
    return records


@st.cache_data(ttl=300)
def load_subawards():
    entity_records = fetch_all_records(ENTITY_TABLE)
    subaward_records = fetch_all_records(SUBAWARDS_TABLE)

    entity_names = {}
    for record in entity_records:
        fields = record.get("fields", {})
        entity_names[record["id"]] = fields.get("Legal / known name") or fields.get("Entity_ID") or record["id"]

    rows = []
    for record in subaward_records:
        fields = record.get("fields", {})
        entity_links = fields.get("Entity") or []
        linked_name = entity_names.get(entity_links[0]) if entity_links else None
        prime_name = linked_name or fields.get("prime_award_recipient") or fields.get("Entity_ID") or "Unknown"
        rows.append(
            {
                "entity": prime_name,
                "entity_id": fields.get("Entity_ID", ""),
                "prime_award_id": fields.get("prime_award_id", ""),
                "subaward_id": fields.get("subaward_id", ""),
                "subawardee": fields.get("subawardee_name") or "Unknown",
                "amount": fields.get("subaward_amount") or 0,
                "action_date": fields.get("action_date"),
                "description": fields.get("description") or "",
                "review_flag": fields.get("review_flag") or "Unknown",
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
    df["action_date"] = pd.to_datetime(df["action_date"], errors="coerce")
    df["fiscal_year"] = df["action_date"].apply(
        lambda value: value.year + 1 if pd.notna(value) and value.month >= 10 else (value.year if pd.notna(value) else None)
    )
    return df


df = load_subawards()

if df.empty:
    st.warning("No subaward records are available yet. Run the USAspending Subawards workflow, then refresh after the cache period.")
    st.stop()

all_entities = sorted(df["entity"].dropna().unique().tolist())
selected_entities = st.multiselect("Prime recipient", all_entities, default=all_entities)

valid_fys = sorted(df["fiscal_year"].dropna().astype(int).unique().tolist())
if valid_fys:
    fy_range = st.slider("Fiscal year range", min_value=min(valid_fys), max_value=max(valid_fys), value=(min(valid_fys), max(valid_fys)))
else:
    fy_range = None

filtered = df[df["entity"].isin(selected_entities)].copy()
if fy_range:
    filtered = filtered[(filtered["fiscal_year"] >= fy_range[0]) & (filtered["fiscal_year"] <= fy_range[1])]

if filtered.empty:
    st.info("No subaward records match the current filters.")
    st.stop()

col1, col2, col3, col4 = st.columns(4)
col1.metric("Reported subaward dollars", f"${filtered['amount'].sum():,.0f}")
col2.metric("Subaward records", f"{len(filtered):,}")
col3.metric("Unique subawardees", f"{filtered['subawardee'].nunique():,}")
col4.metric("Prime recipients", f"{filtered['entity'].nunique():,}")

st.subheader("Money out by prime recipient")
by_prime = filtered.groupby("entity")["amount"].sum().sort_values(ascending=False)
st.bar_chart(by_prime)

st.subheader("Top subawardees")
by_subawardee = filtered.groupby("subawardee")["amount"].sum().sort_values(ascending=False).head(25)
st.bar_chart(by_subawardee)

if filtered["fiscal_year"].notna().any():
    st.subheader("Subaward amounts by fiscal year")
    by_year = filtered.pivot_table(index="fiscal_year", columns="entity", values="amount", aggfunc="sum", fill_value=0).sort_index()
    st.bar_chart(by_year)

st.subheader("Recipient-level detail")
detail = filtered[["entity", "prime_award_id", "subawardee", "subaward_id", "amount", "action_date", "description", "review_flag"]].copy()
detail = detail.sort_values("amount", ascending=False)
detail["amount"] = detail["amount"].map(lambda value: f"${value:,.2f}")
st.dataframe(detail, use_container_width=True, hide_index=True)

st.download_button(
    "Download filtered subawards as CSV",
    data=filtered.to_csv(index=False).encode("utf-8"),
    file_name="subaward_flows.csv",
    mime="text/csv",
)

st.caption("Important: USAspending subaward records report disclosed downstream payments, not necessarily the full value of a prime award. A zero result does not establish that a recipient made no subcontracting payments.")
