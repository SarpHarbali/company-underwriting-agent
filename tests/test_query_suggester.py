from types import SimpleNamespace

from src.companies_house.query_suggester import (
    SuggestedQuery,
    build_query_suggester,
)


def test_query_suggester_uses_configured_model_without_reasoning():
    captured = {}

    def parse(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(output_parsed=SuggestedQuery(query="wetherspoon"))

    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))

    suggest = build_query_suggester(client, "gpt-5.6-luna")

    assert suggest("spoons") == "wetherspoon"
    assert captured["model"] == "gpt-5.6-luna"
    assert captured["reasoning"].effort == "none"
