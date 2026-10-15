from fractions import Fraction
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Span(StrictModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class Candidate(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    presenter: str | None = Field(default=None, max_length=80)
    origin: Literal["visible_text", "explicit_speech", "inferred"]
    lecture: Span
    style_description: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(min_length=1)
    uncertainties: list[str]


class ConfirmedPlan(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    presenter: str | None = Field(default=None, max_length=80)
    lecture: Span
    intro_asset_id: str
    title_asset_id: str | None = None
    title_seconds: float = Field(default=1.5, gt=0, le=10)
    fps: str = Field(default="30/1", pattern=r"^[1-9][0-9]*/[1-9][0-9]*$")
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def fps_range(self):
        if not 1 <= Fraction(self.fps) <= 120:
            raise ValueError("fps outside supported range")
        return self


class ReviewCheck(StrictModel):
    code: str
    decision: Literal["pass", "fail", "uncertain", "not_applicable"]
    reason: str


class Review(StrictModel):
    decision: Literal["pass", "fail", "uncertain"]
    checks: list[ReviewCheck] = Field(min_length=1)
    issues: list[str]
    limits: str = Field(min_length=1)


def review_passed(review: Review, required: set[str]) -> bool:
    codes=[c.code for c in review.checks]
    return review.decision=="pass" and len(codes)==len(set(codes)) and required <= set(codes) and all(c.decision=="pass" for c in review.checks)


def frame_count(seconds: float | str, fps: str) -> int:
    value = Fraction(str(seconds)) * Fraction(fps)
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)
