"""HeadHunter-group API client (hh.ru, rabota.by).

hh.ru and rabota.by share one API at https://api.hh.ru; the `host` query
parameter selects the site ("Выбор сайта" in the API docs), so both sources
use the same code path with a different host and default region.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

import httpx

from .config import SearchFilters

API_BASE = "https://api.hh.ru"
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


class SourceError(RuntimeError):
    """Raised for any failure talking to a job source (HTTP error, timeout, bad JSON)."""


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return SPACE_RE.sub(" ", html.unescape(TAG_RE.sub(" ", text))).strip()


def format_salary(item: dict) -> str:
    salary = item.get("salary_range") or item.get("salary")
    if not salary:
        return ""
    low, high = salary.get("from"), salary.get("to")
    currency = salary.get("currency") or ""
    if low and high:
        amount = f"{low}-{high}"
    elif low:
        amount = f"from {low}"
    elif high:
        amount = f"up to {high}"
    else:
        return ""
    return f"{amount} {currency}".strip()


@dataclass
class Posting:
    source: str
    external_id: str
    title: str
    company: str
    location: str
    salary: str
    snippet: str
    url: str
    published_at: str

    @property
    def key(self) -> str:
        # Both sites serve the same vacancy database, so an ID means the same
        # vacancy on either one. Keying on the ID alone stops a vacancy that
        # matches both searches from being notified twice.
        return f"hh:{self.external_id}"


def posting_from_item(source: str, item: dict) -> Posting:
    snippet = item.get("snippet") or {}
    snippet_text = " ".join(
        part for part in (strip_html(snippet.get("responsibility")), strip_html(snippet.get("requirement"))) if part
    )
    location = (item.get("area") or {}).get("name", "")
    work_format = [w.get("name", "") for w in item.get("work_format") or [] if w.get("name")]
    schedule = (item.get("schedule") or {}).get("name", "")
    extras = work_format or ([schedule] if schedule else [])
    if extras:
        location = f"{location} ({', '.join(extras)})" if location else ", ".join(extras)
    external_id = str(item["id"])
    return Posting(
        source=source,
        external_id=external_id,
        title=item.get("name", "").strip(),
        company=((item.get("employer") or {}).get("name") or "").strip(),
        location=location,
        salary=format_salary(item),
        snippet=snippet_text,
        url=item.get("alternate_url") or f"https://{source}/vacancy/{external_id}",
        published_at=item.get("published_at", ""),
    )


class HHClient:
    def __init__(self, http: httpx.AsyncClient, user_agent: str, access_token: str = ""):
        self.http = http
        self.headers = {"User-Agent": user_agent, "HH-User-Agent": user_agent}
        if access_token:
            self.headers["Authorization"] = f"Bearer {access_token}"

    async def _get(self, path: str, params: list[tuple[str, str]]) -> dict:
        try:
            response = await self.http.get(
                API_BASE + path, params=params, headers=self.headers, timeout=30
            )
        except httpx.HTTPError as exc:
            raise SourceError(f"{path}: {exc.__class__.__name__}: {exc}") from exc
        if response.status_code != 200:
            raise SourceError(f"{path}: HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"{path}: invalid JSON") from exc

    async def search(self, source: str, filters: SearchFilters, per_page: int) -> list[Posting]:
        """Newest postings matching the filters, newest first."""
        params = [
            ("host", source),
            ("order_by", "publication_time"),
            ("per_page", str(per_page)),
            ("page", "0"),
            *filters.to_params(),
        ]
        data = await self._get("/vacancies", params)
        return [posting_from_item(source, item) for item in data.get("items", [])]

    async def description(self, source: str, external_id: str) -> str:
        """Full plain-text description of a single vacancy, for scoring."""
        data = await self._get(f"/vacancies/{external_id}", [("host", source)])
        parts = [strip_html(data.get("description"))]
        skills = [s.get("name", "") for s in data.get("key_skills") or []]
        if skills:
            parts.append("Key skills: " + ", ".join(s for s in skills if s))
        experience = (data.get("experience") or {}).get("name")
        if experience:
            parts.append(f"Experience required: {experience}")
        return "\n\n".join(p for p in parts if p)
