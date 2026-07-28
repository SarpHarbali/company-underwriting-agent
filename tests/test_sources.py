from src.research.sources import SourceRegistry


def test_add_assigns_sequential_ids():
    registry = SourceRegistry()
    id1 = registry.add("web", "Title One", "https://example.com/one")
    id2 = registry.add("web", "Title Two", "https://example.com/two")
    assert id1 == 1
    assert id2 == 2


def test_add_dedupes_by_url():
    registry = SourceRegistry()
    id1 = registry.add("web", "Title One", "https://example.com/one")
    id2 = registry.add("web", "Title One Again", "https://example.com/one")
    assert id1 == id2
    assert len(registry) == 1


def test_valid_ids_and_get():
    registry = SourceRegistry()
    registry.add("companies_house", "Filing history", "https://example.com/filing-history")
    assert registry.valid_ids() == {1}
    source = registry.get(1)
    assert source is not None
    assert source.kind == "companies_house"
    assert registry.get(999) is None


def test_url_deduplication_ignores_tracking_parameters_and_fragments():
    registry = SourceRegistry()
    first = registry.add(
        "web",
        "Tracked",
        "https://EXAMPLE.com/article/?utm_source=search#section",
    )
    second = registry.add("web", "Clean", "https://example.com/article")
    assert first == second
    assert registry.id_for_url("https://example.com/article/") == first
