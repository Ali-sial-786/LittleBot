"""Age-aware safety policy.

These rules are enforced in code (not just in prompts). Age groups are flexible
recommendations for choosing activities, never developmental assessments.
An unknown age is treated as the youngest group (most restrictive) for videos and activities.
Cloud conversation is the parent's choice at any age (``ai.min_age_months_for_cloud`` can set
a minimum), and reply length is the parent's ``ai.max_words``: a grown-up may be the one asking.
"""

from __future__ import annotations

from dataclasses import dataclass

from littlebot.core.interfaces import AGE_GROUPS


def age_group_for(age_months: int | None) -> str:
    if age_months is None:
        return "unknown"
    for key, lo, hi, _label in AGE_GROUPS:
        if lo <= age_months < hi:
            return key
    if age_months < AGE_GROUPS[0][1]:
        return "under_6_months"
    return "older"


def age_group_label(key: str) -> str:
    for k, _lo, _hi, label in AGE_GROUPS:
        if k == key:
            return label
    return {"unknown": "Age not set", "under_6_months": "Under 6 months", "older": "5 years +"}.get(key, key)


@dataclass(frozen=True)
class AgePolicy:
    age_months: int | None
    min_age_months_for_video: int = 18
    min_age_months_for_cloud_ai: int = 0  # 0: any age (also unknown), once a parent switches cloud AI on

    @property
    def effective_age(self) -> int:
        """Age used for restrictions: unknown -> treated as 6 months (most restrictive)."""
        return self.age_months if self.age_months is not None else 6

    @property
    def group(self) -> str:
        return age_group_for(self.age_months)

    def video_allowed(self, parent_override: bool = False) -> bool:
        """Video is off by default under 18 months; parents may explicitly authorise it."""
        return self.effective_age >= self.min_age_months_for_video or parent_override

    def cloud_ai_allowed(self) -> bool:
        """Cloud conversation for this age (a parent must still switch it on). With a minimum age
        set, an unknown age is not allowed."""
        if self.min_age_months_for_cloud_ai <= 0:
            return True
        return self.age_months is not None and self.age_months >= self.min_age_months_for_cloud_ai

    def max_reply_words(self, configured: int = 60) -> int:
        """The reply length cap: the parent's ``ai.max_words``, at any age. The prompt keeps words short
        and simple for a young child; the code can't tell whether the child or a grown-up asked."""
        return max(3, int(configured))

    def activity_suitable(self, min_age: int, max_age: int) -> bool:
        return min_age <= self.effective_age < max_age
