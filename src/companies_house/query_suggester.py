from __future__ import annotations

from openai import OpenAI
from openai.types.shared import Reasoning
from pydantic import BaseModel, ConfigDict

from src.companies_house.resolver import QuerySuggester

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

    query: str | None


def build_query_suggester(client: OpenAI, model: str) -> QuerySuggester:
    def suggest(text: str) -> str | None:
        response = client.responses.parse(
            model=model,
            reasoning=Reasoning(effort="none"),
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
