"""Compatibility scoring: one Claude call per new posting."""

from __future__ import annotations

import anthropic
from pydantic import BaseModel

from .sources import Posting

SYSTEM_PROMPT = """You screen job postings for one candidate, whose resume is below.
For each posting, judge how well the candidate fits the role: skills, seniority,
domain, location and work format. Score 0-100, where 60+ means the candidate
would reasonably apply. The reason is one short sentence naming the main
factors for or against, in English.

<resume>
{resume}
</resume>"""


class Score(BaseModel):
    score: int
    reason: str


class ScoringError(RuntimeError):
    """Transient failure: the posting should be retried on the next cycle."""


class ScoringRefused(RuntimeError):
    """The model declined this posting; retrying would not help."""


class Scorer:
    def __init__(self, api_key: str, model: str, effort: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.effort = effort

    async def score(self, resume: str, posting: Posting, description: str) -> Score:
        header = [f"Title: {posting.title}", f"Company: {posting.company}"]
        if posting.location:
            header.append(f"Location: {posting.location}")
        if posting.salary:
            header.append(f"Salary: {posting.salary}")
        details = "\n".join(header) + "\n\n" + (description or posting.snippet)
        try:
            response = await self.client.messages.parse(
                model=self.model,
                max_tokens=4000,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT.format(resume=resume),
                        # The resume is identical on every call, so cache it.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": details}],
                output_config={"effort": self.effort},
                output_format=Score,
            )
        except anthropic.APIStatusError as exc:
            raise ScoringError(f"Claude API HTTP {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ScoringError(f"Claude API connection error: {exc}") from exc

        if response.stop_reason == "refusal":
            raise ScoringRefused("model declined to score this posting")
        result = response.parsed_output
        if result is None:
            raise ScoringError(f"no structured output (stop_reason={response.stop_reason})")
        result.score = max(0, min(100, result.score))
        return result
