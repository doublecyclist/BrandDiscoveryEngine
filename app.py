import subprocess
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Brand Discovery", layout="wide")

st.title("Brand Discovery")
st.caption("Research names before you invest in a domain, logo, or brand.")

with st.sidebar:
    st.header("Project")
    project_name = st.text_input("Project name", value="Santa Cruz Podcast")
    st.markdown("---")
    st.write("Next features:")
    st.write("• Save projects")
    st.write("• Generate names")
    st.write("• Better scoring")
    st.write("• Export reports")

st.subheader("Candidate Names")

names_text = st.text_area(
    "Paste one name per line",
    value="CruzLocal\nCruz County\nEat Drink Cruz\nHighway One Insider\nWorth the Drive",
    height=250,
)

if st.button("Analyze Names", type="primary"):
    names = [n.strip() for n in names_text.splitlines() if n.strip()]
    pd.DataFrame({"name": names}).to_csv("candidates.csv", index=False)

    with st.spinner(f"Analyzing {len(names)} names..."):
        result = subprocess.run(
            ["py", "brand_engine.py"],
            capture_output=True,
            text=True,
        )

    if result.stderr:
        if "Permission denied" in result.stderr:
            st.error("Close brand_results.xlsx in Excel, then run again.")
        else:
            st.error(result.stderr)
    else:
        st.success("Research complete.")

results_path = Path("brand_results.xlsx")

if results_path.exists():
    st.subheader("Latest Results")

    df = pd.read_excel(results_path, sheet_name="Brand Research")

    cols = [
        "name",
        "initial_score",
        "recommendation",
        "com_dns_exists",
        "apple_podcast_results",
    ]

    display_df = df[cols].sort_values("initial_score", ascending=False)

    st.dataframe(display_df, width="stretch")

    best = display_df.iloc[0]

    st.markdown("### Current Leader")
    st.metric(
        label=best["name"],
        value=int(best["initial_score"]),
        delta=best["recommendation"],
    )