"""Reviewer adapters for AI code review tools."""

from __future__ import annotations

from codereviewbuddy.reviewers.base import ReviewerAdapter
from codereviewbuddy.reviewers.coderabbit import CodeRabbitAdapter
from codereviewbuddy.reviewers.devin import DevinAdapter
from codereviewbuddy.reviewers.registry import REVIEWERS, apply_config, get_reviewer, identify_reviewer
from codereviewbuddy.reviewers.unblocked import UnblockedAdapter

__all__ = [
    "REVIEWERS",
    "CodeRabbitAdapter",
    "DevinAdapter",
    "ReviewerAdapter",
    "UnblockedAdapter",
    "apply_config",
    "get_reviewer",
    "identify_reviewer",
]
