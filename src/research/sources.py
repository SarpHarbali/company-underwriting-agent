"""Registry that assigns a stable numeric ID to every source the agent actually saw.

The final structured-synthesis step is constrained to only cite IDs that exist in
this registry, so a citation in the rendered report always resolves to a real
Companies House page or a URL the web_search tool actually returned - never a
model-invented source.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Source:
    id: int
    kind: str  # "companies_house" | "web"
    title: str
    url: str


@dataclass
class SourceRegistry:
    _sources: list[Source] = field(default_factory=list)
    _url_to_id: dict[str, int] = field(default_factory=dict)

    def add(self, kind: str, title: str, url: str) -> int:
        if url in self._url_to_id:
            return self._url_to_id[url]
        new_id = len(self._sources) + 1
        self._sources.append(Source(id=new_id, kind=kind, title=title, url=url))
        self._url_to_id[url] = new_id
        return new_id

    def get(self, source_id: int) -> Source | None:
        for source in self._sources:
            if source.id == source_id:
                return source
        return None

    def all(self) -> list[Source]:
        return list(self._sources)

    def valid_ids(self) -> set[int]:
        return {s.id for s in self._sources}

    def __len__(self) -> int:
        return len(self._sources)
