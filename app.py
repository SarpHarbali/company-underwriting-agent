"""Streamlit UI for the underwriting intelligence agent."""

from __future__ import annotations

import streamlit as st

from src.orchestrator import Orchestrator
from src.report.builder import build_official_record_markdown

st.set_page_config(page_title="Underwriting Intelligence Agent", layout="wide")


@st.cache_resource
def get_orchestrator() -> Orchestrator:
    return Orchestrator()


def _reset_search_state() -> None:
    st.session_state.resolved_company = None
    st.session_state.candidates = None
    st.session_state.report_markdown = None


st.title("Underwriting Intelligence Agent")
st.caption(
    "Enter a UK company name or Companies House registration number to generate a "
    "citation-backed underwriting intelligence report."
)

for key, default in [("resolved_company", None), ("candidates", None), ("report_markdown", None)]:
    if key not in st.session_state:
        st.session_state[key] = default

try:
    orchestrator = get_orchestrator()
except RuntimeError as exc:
    st.error(str(exc))
    st.stop()

with st.form("search_form"):
    user_input = st.text_input(
        "Company name or registration number",
        placeholder="e.g. Monzo Bank Limited or 09446231",
    )
    submitted = st.form_submit_button("Search")

if submitted:
    _reset_search_state()
    with st.spinner("Looking up Companies House..."):
        result = orchestrator.resolve(user_input)
    if result.error:
        st.error(result.error)
    elif result.is_resolved:
        st.session_state.resolved_company = result.company_profile
    elif result.is_ambiguous:
        st.session_state.candidates = result.candidates

if st.session_state.candidates:
    st.warning(
        f"Found {len(st.session_state.candidates)} possible matches - please confirm "
        "the correct company before a report is generated."
    )
    options = {}
    for c in st.session_state.candidates:
        label = f"{c.title}  ·  {c.company_number}  ·  {c.status}  ·  {c.company_type}"
        if c.date_of_creation:
            label += f"  ·  inc. {c.date_of_creation}"
        if c.address_snippet:
            label += f"  ·  {c.address_snippet}"
        options[label] = c

    choice_label = st.radio("Select the correct company:", list(options.keys()), index=None)
    if choice_label and st.button("Confirm selection"):
        chosen = options[choice_label]
        with st.spinner("Fetching company profile..."):
            st.session_state.resolved_company = orchestrator.get_company_profile(chosen.company_number)
        st.session_state.candidates = None
        st.rerun()

if st.session_state.resolved_company and not st.session_state.candidates:
    profile = st.session_state.resolved_company
    public_url = orchestrator.ch_client.public_company_url(profile["company_number"])

    st.success(
        f"Resolved: **{profile.get('company_name')}** ({profile.get('company_number')}) "
        f"- {profile.get('company_status')}"
    )
    with st.expander("Official Companies House record"):
        st.markdown(build_official_record_markdown(profile, public_url))

    if st.button("Generate underwriting report", type="primary"):
        status_box = st.status("Running research agent...", expanded=True)

        def progress(message: str) -> None:
            status_box.write(message)

        try:
            generated = orchestrator.generate_report(profile, progress=progress)
            status_box.update(label="Research complete", state="complete")
            st.session_state.report_markdown = generated.markdown
        except Exception as exc:  # noqa: BLE001 - surfaced directly to the user
            status_box.update(label="Report generation failed", state="error")
            st.error(f"Something went wrong while generating the report: {exc}")

if st.session_state.report_markdown:
    st.markdown("---")
    st.markdown(st.session_state.report_markdown)
    company_number = (st.session_state.resolved_company or {}).get("company_number", "company")
    st.download_button(
        "Download report (Markdown)",
        data=st.session_state.report_markdown,
        file_name=f"underwriting_report_{company_number}.md",
        mime="text/markdown",
    )

if st.session_state.resolved_company or st.session_state.candidates:
    st.markdown("---")
    if st.button("Start a new search"):
        _reset_search_state()
        st.rerun()
