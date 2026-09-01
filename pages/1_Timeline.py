import os
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Award Timeline", layout="wide")
st.title("Federal Award Timeline")
st.caption("Award activity by fiscal year, per entity, from the Gov_Awards table.")


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
AWARDS_TABLE = get_secret("AIRTABLE_AWARDS_TABLE") or "Gov_Awards"

if not AIRTABLE_PAT or not AIRTABLE_BASE_ID:
    st.error("Missing AIRTABLE_PAT or AIRTABLE_BASE_ID in secrets/environment.")
    st.stop()

HEADERS = {"Authorization": f"Bearer {AIRTABLE_PAT}"}
BASE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"


@st.cache_data(ttl=300)
def fetch_all_records(table_name):
    records = []
    params = {"pageSize": 100}
    url = f"{BASE_URL}/{table_name}"
    while True:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        params["offset"] = offset
    return records


@st.cache_data(ttl=300)
def load_data():
    entity_records = fetch_all_records(ENTITY_TABLE)
    award_records = fetch_all_records(AWARDS_TABLE)

    entity_name_by_id = {}
    for r in entity_records:
        f = r.get("fields", {})
        entity_name_by_id[r["id"]] = f.get("Legal / known name") or f.get("Entity_ID") or r["id"]

    rows = []
    for r in award_records:
        f = r.get("fields", {})
        linked = f.get("Entity") or []
        entity_name = entity_name_by_id.get(linked[0]) if linked else f.get("recipient_name_reported", "Unknown")
        action_date = f.get("action_date")
        amount = f.get("total_obligation") or f.get("transaction_amount") or 0
        if not action_date:
            continue
        rows.append({"entity": entity_name, "action_date": action_date, "amount": amount})

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["action_date"] = pd.to_datetime(df["action_date"], errors="coerce")
    df = df.dropna(subset=["action_date"])
    df["fiscal_year"] = df["action_date"].apply(
        lambda d: d.year + 1 if d.month >= 10 else d.year
    )
    return df


df = load_data()

if df.empty:
    st.warning("No award records with valid action dates were found yet. Run the USAspending sync workflow first.")
    st.stop()

all_entities = sorted(df["entity"].dropna().unique().tolist())
selected_entities = st.multiselect("Filter by entity", options=all_entities, default=all_entities)

min_fy, max_fy = int(df["fiscal_year"].min()), int(df["fiscal_year"].max())
fy_range = st.slider("Fiscal year range", min_value=min_fy, max_value=max_fy, value=(min_fy, max_fy))

filtered = df[
    (df["entity"].isin(selected_entities))
    & (df["fiscal_year"] >= fy_range[0])
    & (df["fiscal_year"] <= fy_range[1])
]

if filtered.empty:
    st.info("No records match the current filters.")
    st.stop()

st.subheader("Total obligations by fiscal year")
pivot = filtered.pivot_table(index="fiscal_year", columns="entity", values="amount", aggfunc="sum", fill_value=0)
st.bar_chart(pivot)

st.subheader("Cumulative obligations over time")
cumulative = pivot.sort_index().cumsum()
st.line_chart(cumulative)

st.subheader("Summary table")
summary = filtered.groupby(["entity", "fiscal_year"])["amount"].sum().reset_index()
summary = summary.sort_values(["entity", "fiscal_year"])
summary["amount"] = summary["amount"].map(lambda x: f"${x:,.0f}")
st.dataframe(summary, use_container_width=True, hide_index=True)

total_by_entity = filtered.groupby("entity")["amount"].sum().sort_values(ascending=False)
st.subheader("Total obligations by entity (selected range)")
st.bar_chart(total_by_entity)
