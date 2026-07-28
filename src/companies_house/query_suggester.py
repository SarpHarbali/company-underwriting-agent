"""LLM-backed suggestion of what a user's search text was *meant* to say.

Strictly a spelling/alias fixer for the query string: the model is asked what
the user was trying to type, never which company they meant. Its answer is fed
back through the same Companies House search and the same deterministic scoring
as any other query, and a corrected query can never auto-resolve (see
`resolve_company`), so the worst a bad suggestion can do is produce a candidate
list the user then rejects.

This exists for the cases single-edit variants can't reach by construction:
multiple typos at once, consonant swaps, and - the real motivation - colloquial
names that differ from the registered one, like "spoons" for Wetherspoon or
"M&S" for Marks and Spencer. No amount of letter-shuffling gets you there.

Called only where it can earn the round trip: when retrieval came back weak and
there is no usable list without a correction, or when the user has looked at a
good list and said their company isn't in it. The prompt's premise that the
search "already failed to find a good match" is therefore true on both paths,
not an assumption.
"""

from __future__ import annotations

from openai import OpenAI
from pydantic import BaseModel, ConfigDict

from src.companies_house.resolver import QuerySuggester

# A suggestion is search text, not a company name, so anything long is a sign
# the model has started inventing a registered name instead of correcting.
_MAX_SUGGESTION_LENGTH = 80

_SYSTEM_PROMPT = """\
You fix UK company-search queries. The text below was typed into a company \
search box and already failed to find a good match, so assume it is \
misspelled, abbreviated, or a nickname - do not assume it is already correct. \
Return the search text the user meant.

Rules:
- Fix misspellings, including ones several letters off: \
"saint laurant" -> "saint laurent", "sainsburys" -> "sainsbury".
- Expand well-known nicknames and short forms to the brand name people would \
search: "spoons" -> "wetherspoon", "M&S" -> "marks and spencer".
- Return the brand name only. Do NOT add legal suffixes (Ltd, Limited, PLC) or \
corporate qualifiers (Holdings, Group, UK) - the search handles those.
- Do NOT guess an official registered name and do NOT invent a company number. \
You are correcting what the user typed, not identifying a company.
- Return null only if the text is meaningless or you genuinely cannot tell what \
was meant. A wrong guess is worse than no guess, but declining to correct an \
obvious misspelling is also a failure.\
"""


class SuggestedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Null when the model has no confident reading of the text - see the
    # prompt's "a wrong guess is worse than no guess".
    query: str | None


def build_query_suggester(client: OpenAI, model: str) -> QuerySuggester:
    """Returns a callable matching resolver.QuerySuggester.

    Kept as a closure rather than a class so `resolve_company` stays testable
    with a plain lambda and never has to import OpenAI.
    """

    def suggest(text: str) -> str | None:
        response = client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            text_format=SuggestedQuery,
        )
        parsed = response.output_parsed
        if parsed is None or not parsed.query:
            return None

        suggestion = parsed.query.strip()
        if not suggestion or len(suggestion) > _MAX_SUGGESTION_LENGTH:
            return None
        return suggestion

    return suggest
