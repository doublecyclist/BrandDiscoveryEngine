import subprocess
from pathlib import Path

import pandas as pd
import streamlit as st

from config import (
    APP_TITLE,
    APP_SUBTITLE,
    DEFAULT_PROJECT,
    PROJECT_FOLDER,
)


def safe_filename(name: str) -> str:
    return name.strip().replace(" ", "_").replace("/", "_")


st.set_page_config(page_title=APP_TITLE, layout="wide")

PROJECTS_DIR = Path(PROJECT_FOLDER)
PROJECTS_DIR.mkdir(exist_ok=True)

st.title(APP_TITLE)
st.caption(APP_SUBTITLE)

with st.sidebar:
    st.header("Project")

    project_name = st.text_input("Project name", value=DEFAULT_PROJECT)
    project_file = PROJECTS_DIR / f"{safe_filename(project_name)}.csv"

    existing_projects = sorted(PROJECTS_DIR.glob("*.csv"))

    if existing_projects:
        selected_project = st.selectbox(
            "Load existing project",
            [""] + [p.stem.replace("_", " ") for p in existing_projects],
        )

        if selected_project:
            load_file = PROJECTS_DIR / f"{safe_filename(selected_project)}.csv"
            if load_file.exists():
                loaded_df = pd.read_csv(load_file)
                st.session_state["loaded_names"] = "\n".join(
                    loaded_df["name"].dropna().tolist()
                )
                st.success(f"Loaded {selected_project}")

    st.markdown("---")
    st.write("Next features:")
    st.write("• Generate names")
    st.write("• Better scoring")
    st.write("• Export reports")


st.subheader("Candidate Names")

default_names = st.session_state.get(
    "loaded_names",
    "CruzLocal\nCruz County\nEat Drink Cruz\nHighway One Insider\nWorth the Drive",
)

names_text = st.text_area(
    "Paste one name per line",
    value=default_names,
    height=250,
)

col1, col2 = st.columns([1, 1])

with col1:
    if st.button("Save Project"):
        names = [n.strip() for n in names_text.splitlines() if n.strip()]
        pd.DataFrame({"name": names}).to_csv(project_file, index=False)
        st.success(f"Saved project: {project_name}")

with col2:
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

    if not display_df.empty:
        best = display_df.iloc[0]

        st.markdown("### Current Leader")
        st.metric(
            label=best["name"],
            value=int(best["initial_score"]),
            delta=best["recommendation"],
        )