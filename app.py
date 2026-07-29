from __future__ import annotations

import streamlit as st

from src.companies_house.ranking import Candidate
from src.orchestrator import Orchestrator
from src.report.builder import build_official_record_markdown

st.set_page_config(page_title="Underwriting Intelligence Agent", layout="wide")

_SESSION_DEFAULTS = {
    "resolved_company": None,
    "candidates": None,
    "corrected_query": None,
    "search_text": "",
    "can_suggest": False,
    "suggestion": None,
    "showing_suggestion": False,
    "no_suggestion_found": False,
    "report_markdown": None,
}


@st.cache_resource
def get_orchestrator() -> Orchestrator:
    return Orchestrator()


def _reset_search_state() -> None:
    for key, default in _SESSION_DEFAULTS.items():
        st.session_state[key] = default


def _render_candidates(
    orchestrator: Orchestrator,
    candidates: list[Candidate],
    key_prefix: str,
) -> None:
    for position, candidate in enumerate(candidates):
        with st.container(border=True):
            name_col, action_col = st.columns([5, 1])
            with name_col:
                title_line = f"**{candidate.title}**"
                if position == 0:
                    title_line += "  🏆 *Best match*"
                st.markdown(title_line)

                if candidate.is_previous_name:
                    st.caption(
                        f"↩︎ matched on former name: *{candidate.matched_name}*"
                    )

                status_dot = "🟢" if candidate.status == "active" else "⚪"
                meta = [
                    f"{status_dot} Status: {candidate.status}",
                    f"Company no.: {candidate.company_number}",
                ]
                if candidate.incorporation_date:
                    meta.append(
                        f"Incorporated: {candidate.incorporation_date.isoformat()}"
                    )
                if candidate.postcode:
                    meta.append(f"Postcode: {candidate.postcode}")
                st.caption("  ·  ".join(meta))
            with action_col:
                if st.button(
                    "Select",
                    key=f"{key_prefix}_{candidate.company_number}",
                ):
                    with st.spinner("Fetching company profile..."):
                        st.session_state.resolved_company = (
                            orchestrator.get_company_profile(
                                candidate.company_number
                            )
                        )
                    st.session_state.candidates = None
                    st.session_state.can_suggest = False
                    st.session_state.suggestion = None
                    st.session_state.showing_suggestion = False
                    st.session_state.no_suggestion_found = False
                    st.rerun()


st.title("Underwriting Intelligence Agent")
st.caption(
    "Enter a UK company name or Companies House registration number to generate a "
    "citation-backed underwriting intelligence report."
)

for key, default in _SESSION_DEFAULTS.items():
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
        placeholder="e.g. Allica Bank Limited or 07706156",
    )
    submitted = st.form_submit_button("Search")

if submitted:
    _reset_search_state()
    with st.spinner("Searching the Companies House register..."):
        result = orchestrator.resolve(user_input)
    if result.error:
        st.error(result.error)
    elif result.is_resolved:
        st.session_state.resolved_company = result.company_profile
    elif result.is_ambiguous:
        st.session_state.candidates = result.candidates
        st.session_state.corrected_query = result.corrected_query
        st.session_state.search_text = user_input
        st.session_state.can_suggest = result.can_suggest

if st.session_state.can_suggest and not st.session_state.suggestion:
    prompt_col, yes_col = st.columns([5, 1])
    with prompt_col:
        st.info("Can't find what you're looking for?")
    with yes_col:
        if st.button("Yes", key="ask_suggestion"):
            with st.spinner("Looking for another reading of your search..."):
                st.session_state.suggestion = orchestrator.suggest_alternative(
                    st.session_state.search_text,
                    [c.company_number for c in st.session_state.candidates],
                )
            st.session_state.can_suggest = False
            st.session_state.showing_suggestion = st.session_state.suggestion is not None
            st.session_state.no_suggestion_found = st.session_state.suggestion is None
            st.rerun()

if st.session_state.no_suggestion_found:
    st.info(
        "No other reading of that search turned up anything - try a different "
        "spelling, the full registered name, or the registration number."
    )

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
                f"No convincing matches for that spelling - showing results for "
                f"**'{st.session_state.corrected_query}'** instead. Please confirm "
                "the right company below."
            )

    match_count = len(shown_candidates)
    st.warning(
        f"Found {match_count} possible "
        f"{'match' if match_count == 1 else 'matches'} - please confirm "
        "the correct company before a report is generated."
    )
    _render_candidates(orchestrator, shown_candidates, key_prefix="select")

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
