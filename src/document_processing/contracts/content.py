"""Model-authored drafts and system-enriched page content."""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

from .base import EmptyOrNonBlankText, NonBlankText, StrictContract

TopicId = Annotated[
    str,
    StringConstraints(pattern=r"^p[0-9]{4,}-t[0-9]{3,}$"),
]
AssetId = Annotated[
    str,
    StringConstraints(pattern=r"^p[0-9]{4,}-a[0-9]{3,}$"),
]


class TopicDraft(StrictContract):
    """Topic fields the page model is allowed to author."""

    topic_name: NonBlankText
    topic_description: NonBlankText


class AssetDraft(StrictContract):
    """Asset fields the page model is allowed to author."""

    asset_type: NonBlankText
    asset_name: EmptyOrNonBlankText
    asset_description: NonBlankText


class Topic(StrictContract):
    """Persisted topic with a system-assigned stable identifier."""

    topic_id: TopicId
    topic_name: NonBlankText
    topic_description: NonBlankText


class Asset(StrictContract):
    """Persisted asset with a system-assigned stable identifier."""

    asset_id: AssetId
    asset_type: NonBlankText
    asset_name: EmptyOrNonBlankText
    asset_description: NonBlankText
