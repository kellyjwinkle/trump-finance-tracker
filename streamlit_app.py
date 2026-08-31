import os
import json
from datetime import datetime

import requests
import pandas as pd
import streamlit as st
import networkx as nx
from pyvis.network import Network
import streamlit.components.v1 as components

st.set_page_config(
    page_title="Trump Finance Tracker",
    page_icon="\U0001F5C3\uFE0F",
    layout="wide",
)

AIRTABLE_PAT = st.secrets.get("AIRTABLE_PAT", os.getenv("AIRTABLE_PAT", ""))
AIRTABLE_BASE_ID = st.secrets.get("AIRTABLE_BASE_ID", os.getenv("AIRTABLE_BASE_ID", ""))
BASE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"

HEADERS = {"Authorization": f"Bearer {AIRTABLE_PAT}"}

EVIDENCE_COLOR = {
    "Official filing": "#1a7a3c",
    "Government record": "#1a7a3c",
    "Court record": "#1a7a3c",
    "Company disclosure": "#7a6a1a",
    "Investigative reporting": "#b06a00",
    "Allegation/complaint": "#a12c2c",
    "Unverified": "#666666",
}

ENTITY_TYPE_COLOR = {
    "Private company": "#4f81bd",
    "Public company": "#2e5c8a",
    "Fund": "#8064a2",
    "Nonprofit": "#4bacc6",
    "Trust": "#9bbb59",
    "Government agency": "#c0504d",
    "Person": "#f79646",
    "Shell/merger vehicle": "#a6a6a6",
}


@st.cache_data(ttl=300)
def fetch_table(table_name: str) -> pd.DataFrame:
    records = []
    offset = None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        resp = requests.get(f"{BASE_URL}/{table_name}", headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for rec in data.get("records", []):
            row = dict(rec.get("fields", {}))
            row["_id"] = rec["id"]
            records.append(row)
        offset = data.get("offset")
        if not offset:
            break
    return pd.DataFrame(records)


def money(value) -> str:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "\u2014"
    if abs(value) >= 1_000_000_000:
        return f"${value/1_000_000_000:,.2f}B"
    if abs(value) >= 1_000_000:
        return f"${value/1_000_000:,.2f}M"
    if abs(value) >= 1_000:
        return f"${value/1_000:,.1f}K"
    return f"${value:,.0f}"


st.title("Trump Finance Tracker")
st.caption(
    "Research dashboard tracing documented ownership/investment relationships and federal award data. "
    "Reported claims are distinguished from documented facts throughout \u2014 check the Evidence level column."
)

if not AIRTABLE_PAT or not AIRTABLE_BASE_ID:
    st.error(
        "Missing Airtable credentials. Set AIRTABLE_PAT and AIRTABLE_BASE_ID in Streamlit secrets "
        "(Settings \u2192 Secrets) or as environment variables."
    )
    st.stop()

with st.spinner("Loading data from Airtable..."):
    try:
        entities = fetch_table("Entity")
        awards = fetch_table("Gov_Awards")
        relationships = fetch_table("Relationships")
        sources = fetch_table("Sources")
    except requests.exceptions.HTTPError as exc:
        st.error(f"Failed to load data from Airtable: {exc}")
        st.stop()

for df, cols in [
    (entities, ["Entity_ID", "Legal / known name", "Entity type", "Verification status",
                "Total federal obligations", "Total award transactions", "Award count"]),
    (awards, ["Entity_ID", "award_id", "awarding_agency", "recipient_name_reported",
              "transaction_amount", "total_obligation", "award_ceiling", "action_date", "review_flag"]),
    (relationships, ["Relationship_ID", "Relationship type", "Evidence level", "Claim status", "Notes"]),
    (sources, ["Source title", "Source type", "Publisher / agency", "URL"]),
]:
    for c in cols:
        if c not in df.columns:
            df[c] = None

total_entities = len(entities)
total_relationships = len(relationships)
total_awards_value = pd.to_numeric(awards["transaction_amount"], errors="coerce").fillna(0).sum()
total_award_records = len(awards)

k1, k2, k3, k4 = st.columns(4)
k1.metric("Tracked entities", total_entities)
k2.metric("Documented relationships", total_relationships)
k3.metric("Award records", total_award_records)
k4.metric("Total tracked award value", money(total_awards_value))

st.divider()

tab_network, tab_awards, tab_relationships, tab_entities, tab_sources = st.tabs(
    ["Network Map", "Federal Awards", "Relationships", "Entities", "Sources"]
)

with tab_network:
    st.subheader("Entity relationship network")
    st.caption(
        "Node color = entity type. Edge color = evidence level "
        "(green = official/court record, gold = company disclosure, orange = reported/investigative, "
        "red = allegation, gray = unverified)."
    )

    if relationships.empty or entities.empty:
        st.info("No relationship data yet.")
    else:
        id_to_name = dict(zip(entities["_id"], entities["Legal / known name"]))
        id_to_type = dict(zip(entities["_id"], entities["Entity type"]))

        g = nx.DiGraph()
        for _, row in entities.iterrows():
            g.add_node(
                row["_id"],
                label=row.get("Legal / known name") or row["_id"],
                title=f"{row.get('Legal / known name')} ({row.get('Entity type')})",
                color=ENTITY_TYPE_COLOR.get(row.get("Entity type"), "#cccccc"),
            )

        for _, row in relationships.iterrows():
            from_ids = row.get("From entity") or []
            to_ids = row.get("To entity") or []
            if not isinstance(from_ids, list) or not isinstance(to_ids, list):
                continue
            for f in from_ids:
                for t in to_ids:
                    if f in g.nodes and t in g.nodes:
                        evidence = row.get("Evidence level") or "Unverified"
                        g.add_edge(
                            f, t,
                            title=f"{row.get('Relationship type')} \u2014 {row.get('Claim status')}<br>{row.get('Notes','')}",
                            color=EVIDENCE_COLOR.get(evidence, "#888888"),
                            label=row.get("Relationship type", ""),
                        )

        net = Network(height="650px", width="100%", directed=True, bgcolor="#ffffff", font_color="#222222")
        net.from_nx(g)
        net.set_options("""
        var options = {
          "nodes": {"shape": "dot", "size": 14, "font": {"size": 13}},
          "edges": {"arrows": {"to": {"enabled": true}}, "smooth": {"type": "cubicBezier"}},
          "physics": {"stabilization": {"iterations": 150}, "barnesHut": {"gravitationalConstant": -8000, "springLength": 180}}
        }
        """)
        html_path = "/tmp/network.html"
        net.save_graph(html_path)
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        components.html(html_content, height=670, scrolling=True)

with tab_awards:
    st.subheader("Federal award records")
    entity_options = ["All"] + sorted(entities["Legal / known name"].dropna().unique().tolist())
    selected_entity = st.selectbox("Filter by entity", entity_options)

    display_awards = awards.copy()
    if selected_entity != "All":
        eid = entities.loc[entities["Legal / known name"] == selected_entity, "Entity_ID"]
        if not eid.empty:
            display_awards = display_awards[display_awards["Entity_ID"] == eid.iloc[0]]

    if display_awards.empty:
        st.info("No award records for this selection yet.")
    else:
        show_cols = ["Entity_ID", "award_id", "award_type", "awarding_agency", "recipient_name_reported",
                     "action_date", "transaction_amount", "total_obligation", "award_ceiling", "review_flag"]
        show_cols = [c for c in show_cols if c in display_awards.columns]
        st.dataframe(display_awards[show_cols], use_container_width=True, hide_index=True)

        total_val = pd.to_numeric(display_awards["transaction_amount"], errors="coerce").fillna(0).sum()
        st.metric("Total for selection", money(total_val))

with tab_relationships:
    st.subheader("Documented and reported relationships")
    evidence_filter = st.multiselect(
        "Filter by evidence level",
        options=sorted(relationships["Evidence level"].dropna().unique().tolist()),
        default=None,
    )
    display_rel = relationships.copy()
    if evidence_filter:
        display_rel = display_rel[display_rel["Evidence level"].isin(evidence_filter)]

    def resolve_names(id_list):
        if not isinstance(id_list, list):
            return ""
        names = entities.set_index("_id")["Legal / known name"].to_dict()
        return ", ".join(names.get(i, i) for i in id_list)

    if not display_rel.empty:
        display_rel = display_rel.copy()
        display_rel["From"] = display_rel.get("From entity", pd.Series([[]] * len(display_rel))).apply(resolve_names)
        display_rel["To"] = display_rel.get("To entity", pd.Series([[]] * len(display_rel))).apply(resolve_names)
        show_cols = ["Relationship_ID", "From", "To", "Relationship type", "Ownership_pct",
                     "Evidence level", "Claim status", "Notes"]
        show_cols = [c for c in show_cols if c in display_rel.columns]
        st.dataframe(display_rel[show_cols], use_container_width=True, hide_index=True)
    else:
        st.info("No relationships match this filter.")

with tab_entities:
    st.subheader("Entity registry")
    show_cols = ["Entity_ID", "Legal / known name", "Entity type", "Verification status",
                 "Jurisdiction", "Total federal obligations", "Total award transactions",
                 "Award count", "Notes"]
    show_cols = [c for c in show_cols if c in entities.columns]
    st.dataframe(entities[show_cols], use_container_width=True, hide_index=True)

with tab_sources:
    st.subheader("Source registry")
    show_cols = ["Source title", "Source type", "Publisher / agency", "Primary or secondary",
                 "Publication date", "URL", "Claim supported"]
    show_cols = [c for c in show_cols if c in sources.columns]
    st.dataframe(sources[show_cols], use_container_width=True, hide_index=True)

st.divider()
st.caption(
    f"Data refreshed from Airtable base `{AIRTABLE_BASE_ID}` on page load (cached 5 minutes). "
    f"Last app render: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}."
)
