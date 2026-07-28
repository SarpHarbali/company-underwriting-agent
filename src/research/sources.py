"""Stable source IDs for citation validation and report rendering."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}


def canonical_url(url: str) -> str:
    """Normalise harmless URL differences before citation validation."""
    value = url.strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value
    query = urlencode(
        [
            (key, val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_QUERY_KEYS
            and not key.lower().startswith("utm_")
        ]
    )
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, query, ""))


@dataclass(frozen=True)
class Source:
    id: int
    kind: str
    title: str
    url: str


@dataclass
class SourceRegistry:
    _sources: list[Source] = field(default_factory=list)
    _url_to_id: dict[str, int] = field(default_factory=dict)

    def add(self, kind: str, title: str, url: str) -> int:
        canonical = canonical_url(url)
        if canonical in self._url_to_id:
            existing_id = self._url_to_id[canonical]
            existing = self._sources[existing_id - 1]
            if existing.title == existing.url and title and title != url:
                self._sources[existing_id - 1] = replace(existing, title=title)
            return existing_id
        new_id = len(self._sources) + 1
        self._sources.append(Source(id=new_id, kind=kind, title=title, url=url))
        self._url_to_id[canonical] = new_id
        return new_id

    def id_for_url(self, url: str) -> int | None:
        return self._url_to_id.get(canonical_url(url))

    def get(self, source_id: int) -> Source | None:
        if 1 <= source_id <= len(self._sources):
            return self._sources[source_id - 1]
        return None

    def all(self) -> list[Source]:
        return list(self._sources)

    def valid_ids(self) -> set[int]:
        return {source.id for source in self._sources}

    def __len__(self) -> int:
        return len(self._sources)
