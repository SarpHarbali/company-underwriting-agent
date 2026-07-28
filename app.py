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
    st.session_state.corrected_query = None
    st.session_state.suggestion = None
    st.session_state.showing_suggestion = False
    st.session_state.report_markdown = None


def _render_candidates(candidates, key_prefix: str) -> None:
    """Render a selectable list of candidates, best first."""
    for i, c in enumerate(candidates):
        with st.container(border=True):
            name_col, action_col = st.columns([5, 1])
            with name_col:
                title_line = f"**{c.title}**"
                if i == 0:
                    title_line += "  🏆 *Best match*"
                st.markdown(title_line)

                status_dot = "🟢" if c.status == "active" else "⚪"
                meta = [f"{status_dot} {c.status}", c.company_type, f"No. {c.company_number}"]
                if c.date_of_creation:
                    meta.append(f"inc. {c.date_of_creation}")
                st.caption("  ·  ".join(meta))
                if c.address_snippet:
                    st.caption(c.address_snippet)
            with action_col:
                if st.button("Select", key=f"{key_prefix}_{c.company_number}"):
                    with st.spinner("Fetching company profile..."):
                        st.session_state.resolved_company = orchestrator.get_company_profile(c.company_number)
                    st.session_state.candidates = None
                    st.session_state.suggestion = None
                    st.session_state.showing_suggestion = False
                    st.rerun()


st.title("Underwriting Intelligence Agent")
st.caption(
    "Enter a UK company name or Companies House registration number to generate a "
    "citation-backed underwriting intelligence report."
)

for key, default in [
    ("resolved_company", None),
    ("candidates", None),
    ("corrected_query", None),
    ("suggestion", None),
    ("showing_suggestion", False),
    ("report_markdown", None),
]:
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
        st.session_state.corrected_query = result.corrected_query
        st.session_state.suggestion = result.suggestion

# Offered right under the search bar, but only as a question - the suggested
# query's results stay hidden until asked for, so a wrong guess costs the user
# a glance rather than a screen of irrelevant companies.
if st.session_state.suggestion and not st.session_state.showing_suggestion:
    prompt_col, yes_col = st.columns([5, 1])
    with prompt_col:
        st.info(f"Did you mean **{st.session_state.suggestion.query}**?")
    with yes_col:
        if st.button("Yes", key="show_suggestion"):
            st.session_state.showing_suggestion = True
            st.rerun()

if st.session_state.candidates:
    showing_suggestion = st.session_state.showing_suggestion and st.session_state.suggestion
    if showing_suggestion:
        shown_candidates = st.session_state.suggestion.candidates
        # Without a way back, a wrong guess would strand the user on results
        # for a query they never typed, with only a re-search to escape.
        msg_col, back_col = st.columns([5, 1])
        with msg_col:
            st.info(f"Showing results for **{st.session_state.suggestion.query}**.")
        with back_col:
            if st.button("Back", key="hide_suggestion"):
                st.session_state.showing_suggestion = False
                st.rerun()
    else:
        shown_candidates = st.session_state.candidates
        if st.session_state.corrected_query:
            st.info(
                f"No matches for that spelling - showing results for "
                f"**'{st.session_state.corrected_query}'** instead. Please confirm the right company below."
            )

    match_count = len(shown_candidates)
    st.warning(
        f"Found {match_count} possible {'match' if match_count == 1 else 'matches'} - please confirm "
        "the correct company before a report is generated."
    )
    _render_candidates(shown_candidates, key_prefix="select")

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
