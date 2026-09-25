import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from job_alert.app import JobAlertBot
from job_alert.config import Config, SearchFilters
from job_alert.db import Store
from job_alert.scorer import Score, ScoringError
from job_alert.telegram import escape_markdown, format_notification
from job_alert.sources import posting_from_item


def vacancy(vid, name="Python Developer", host="hh.ru"):
    return {
        "id": str(vid),
        "name": name,
        "employer": {"name": "Acme_Corp"},
        "area": {"name": "Minsk"},
        "salary": {"from": 2500, "to": 3500, "currency": "USD"},
        "snippet": {"requirement": "<highlighttext>Python</highlighttext> 3+ years", "responsibility": "Build APIs"},
        "alternate_url": f"https://{host}/vacancy/{vid}",
        "published_at": "2026-09-25T08:00:00+0300",
    }


class FakeWorld:
    """Stands in for api.hh.ru and api.telegram.org."""

    def __init__(self):
        self.listings = {"hh.ru": [], "rabota.by": []}
        self.failing_hosts = set()
        self.sent = []
        self.telegram_down = False
        self.search_params = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        if url.netloc == "api.hh.ru":
            params = parse_qs(url.query)
            host = params["host"][0]
            if host in self.failing_hosts:
                return httpx.Response(503, text="down")
            if url.path == "/vacancies":
                self.search_params.append(params)
                return httpx.Response(200, json={"items": self.listings[host]})
            return httpx.Response(200, json={"description": "<p>Full description</p>", "key_skills": [{"name": "Django"}]})
        if url.netloc == "api.telegram.org":
            if self.telegram_down:
                return httpx.Response(502, json={"ok": False, "error_code": 502, "description": "Bad Gateway"})
            body = json.loads(request.content)
            self.sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {}})
        raise AssertionError(f"unexpected request {request.url}")


class FakeScorer:
    def __init__(self, scores):
        self.scores = scores  # external_id -> int, or Exception
        self.calls = []

    async def score(self, resume, posting, description):
        self.calls.append(posting.external_id)
        value = self.scores.get(posting.external_id, 80)
        if isinstance(value, Exception):
            raise value
        return Score(score=value, reason="Good Python fit")


def make_config(tmp_path: Path, **overrides) -> Config:
    filters = SearchFilters(text="python", area="", experience="", professional_roles=["96"])
    values = dict(
        telegram_bot_token="TOKEN", telegram_chat_id=42, anthropic_api_key="key",
        claude_model="claude-opus-5", claude_effort="low", hh_access_token="",
        hh_user_agent="test/1.0 (me@example.com)", sources=["hh.ru", "rabota.by"],
        filters={"hh.ru": filters, "rabota.by": filters}, poll_interval_seconds=300,
        per_page=50, first_run_batch=2, threshold=60, heartbeat_hour=9,
        timezone="Europe/Minsk", data_dir=tmp_path, resume_file=tmp_path / "resume.txt",
        resume_text="Senior Python developer, 6 years of Django, PostgreSQL and Kubernetes.",
    )
    values.update(overrides)
    return Config(**values)


@pytest.fixture
def world():
    return FakeWorld()


def make_bot(tmp_path, world, scorer, **overrides):
    http = httpx.AsyncClient(transport=httpx.MockTransport(world.handler))
    store = Store(tmp_path / "db.sqlite3")
    return JobAlertBot(make_config(tmp_path, **overrides), store, http, scorer=scorer)


def run(coro):
    return asyncio.run(coro)


def test_first_run_scores_capped_batch_and_marks_rest_seen(tmp_path, world, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    world.listings["hh.ru"] = [vacancy(i) for i in (5, 4, 3, 2, 1)]
    scorer = FakeScorer({})
    bot = make_bot(tmp_path, world, scorer)

    run(bot.poll_once())
    assert scorer.calls == ["5", "4"]
    assert len(world.sent) == 2

    # A new posting on the next cycle is scored; old ones are not re-scored.
    world.listings["hh.ru"].insert(0, vacancy(6))
    run(bot.poll_once())
    assert scorer.calls == ["5", "4", "6"]
    assert len(world.sent) == 3


def test_below_threshold_is_recorded_but_not_sent(tmp_path, world, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    world.listings["hh.ru"] = [vacancy(1), vacancy(2)]
    scorer = FakeScorer({"1": 42, "2": 85})
    bot = make_bot(tmp_path, world, scorer)
    run(bot.poll_once())
    run(bot.poll_once())
    assert scorer.calls == ["1", "2"]
    assert [m["text"].splitlines()[0] for m in world.sent] == ["🆕 New match: 85% compatible"]


def test_scoring_failure_is_retried_next_cycle(tmp_path, world, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    world.listings["hh.ru"] = [vacancy(1)]
    scorer = FakeScorer({"1": ScoringError("timeout")})
    bot = make_bot(tmp_path, world, scorer)
    run(bot.poll_once())
    assert world.sent == []
    scorer.scores["1"] = 90
    run(bot.poll_once())
    assert scorer.calls == ["1", "1"]
    assert len(world.sent) == 1


def test_same_vacancy_on_both_sites_notifies_once(tmp_path, world, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    world.listings["hh.ru"] = [vacancy(7)]
    world.listings["rabota.by"] = [vacancy(7, host="rabota.by")]
    scorer = FakeScorer({})
    bot = make_bot(tmp_path, world, scorer)
    run(bot.poll_once())
    assert scorer.calls == ["7"]
    assert len(world.sent) == 1


def test_one_source_down_does_not_stop_the_other(tmp_path, world, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    world.failing_hosts.add("hh.ru")
    world.listings["rabota.by"] = [vacancy(9, host="rabota.by")]
    bot = make_bot(tmp_path, world, FakeScorer({}))
    run(bot.poll_once())
    assert len(world.sent) == 1
    assert bot.source_state["hh.ru"].failures == 1
    assert bot.source_state["rabota.by"].failures == 0


def test_telegram_failure_resends_without_rescoring(tmp_path, world, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    world.listings["hh.ru"] = [vacancy(1)]
    scorer = FakeScorer({})
    bot = make_bot(tmp_path, world, scorer)
    world.telegram_down = True
    run(bot.poll_once())
    world.telegram_down = False
    run(bot.poll_once())
    assert scorer.calls == ["1"]
    assert len(world.sent) == 1


def test_first_start_registers_owner_and_ignores_strangers(tmp_path, world, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    bot = make_bot(tmp_path, world, FakeScorer({}), telegram_chat_id=None)
    run(bot.handle_message({"chat": {"id": 100}, "text": "hello"}))
    assert bot.chat_id is None
    run(bot.handle_message({"chat": {"id": 100}, "text": "/start"}))
    assert bot.chat_id == 100
    run(bot.handle_message({"chat": {"id": 200}, "text": "/start"}))
    assert bot.chat_id == 100
    assert all(m["chat_id"] == 100 for m in world.sent)


def test_resume_command_stores_resume(tmp_path, world, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    bot = make_bot(tmp_path, world, FakeScorer({}), resume_text="")
    assert bot.resume() == ""
    text = "Python developer with Django, FastAPI, PostgreSQL, Docker and Kubernetes experience."
    run(bot.handle_message({"chat": {"id": 42}, "text": "/resume " + text}))
    assert bot.resume() == text


def test_no_resume_means_nothing_scored_or_marked(tmp_path, world, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    world.listings["hh.ru"] = [vacancy(1)]
    scorer = FakeScorer({})
    bot = make_bot(tmp_path, world, scorer, resume_text="")
    run(bot.poll_once())
    assert scorer.calls == []
    bot.store.set_setting("resume", "Senior Python developer " * 5)
    run(bot.poll_once())
    assert scorer.calls == ["1"]


def test_search_uses_host_and_filters(tmp_path, world, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    bot = make_bot(tmp_path, world, FakeScorer({}))
    run(bot.poll_once())
    hosts = [p["host"][0] for p in world.search_params]
    assert hosts == ["hh.ru", "rabota.by"]
    assert world.search_params[0]["professional_role"] == ["96"]
    assert world.search_params[0]["order_by"] == ["publication_time"]


def test_notification_format_escapes_markdown():
    posting = posting_from_item("hh.ru", vacancy(12345, name="Senior *Backend* Dev"))
    text = format_notification(posting, 85, "Strong match on Python_3")
    assert text.splitlines() == [
        "🆕 New match: 85% compatible",
        "*Senior \\*Backend\\* Dev* @ Acme\\_Corp",
        "📍 Minsk | 💰 2500-3500 USD",
        "Build APIs Python 3+ years",
        "✅ Why it matches: Strong match on Python\\_3",
        "🔗 [View posting](https://hh.ru/vacancy/12345)",
    ]
    assert escape_markdown("a[b") == "a\\[b"


def test_heartbeat_sent_once_per_day(tmp_path, world, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    bot = make_bot(tmp_path, world, FakeScorer({}), heartbeat_hour=0)
    bot.store.bump("checks", 5)
    run(bot.heartbeat_once())
    run(bot.heartbeat_once())
    assert len(world.sent) == 1
    assert "5 checks" in world.sent[0]["text"]
    assert bot.store.counters()["checks"] == 0


async def _no_sleep(_seconds):
    return None
