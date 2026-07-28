from src.config import load_settings


def test_default_models_are_routed_by_role(monkeypatch):
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "ch-test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test")
    monkeypatch.delenv("OPENAI_RESEARCH_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_QUERY_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_AUDITOR_MODEL", raising=False)

    settings = load_settings()

    assert settings.openai_research_model == "gpt-5.6-luna"
    assert settings.openai_query_model == "gpt-5.6-luna"
    assert settings.openai_auditor_model == "gpt-5.6-terra"
