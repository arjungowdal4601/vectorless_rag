"""Deterministic, system-owned topic and asset identifiers."""

from __future__ import annotations

from collections.abc import Sequence

from .content import Asset, AssetDraft, Topic, TopicDraft


def _require_page_number(page_number: int) -> None:
    if isinstance(page_number, bool) or not isinstance(page_number, int):
        raise TypeError("page_number must be an integer")
    if page_number < 1:
        raise ValueError("page_number must be at least 1")


def topic_id(page_number: int, ordinal: int) -> str:
    _require_page_number(page_number)
    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        raise TypeError("ordinal must be an integer")
    if ordinal < 1:
        raise ValueError("ordinal must be at least 1")
    return f"p{page_number:04d}-t{ordinal:03d}"


def asset_id(page_number: int, ordinal: int) -> str:
    _require_page_number(page_number)
    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        raise TypeError("ordinal must be an integer")
    if ordinal < 1:
        raise ValueError("ordinal must be at least 1")
    return f"p{page_number:04d}-a{ordinal:03d}"


def assign_topic_ids(
    page_number: int,
    topics: Sequence[TopicDraft],
) -> list[Topic]:
    _require_page_number(page_number)
    return [
        Topic(
            topic_id=topic_id(page_number, ordinal),
            topic_name=topic.topic_name,
            topic_description=topic.topic_description,
        )
        for ordinal, topic in enumerate(topics, start=1)
    ]


def assign_asset_ids(
    page_number: int,
    assets: Sequence[AssetDraft],
) -> list[Asset]:
    _require_page_number(page_number)
    return [
        Asset(
            asset_id=asset_id(page_number, ordinal),
            asset_type=asset.asset_type,
            asset_name=asset.asset_name,
            asset_description=asset.asset_description,
        )
        for ordinal, asset in enumerate(assets, start=1)
    ]
