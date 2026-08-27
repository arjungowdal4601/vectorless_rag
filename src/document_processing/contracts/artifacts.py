"""Final page-level knowledge artifacts."""

from __future__ import annotations

from pydantic import model_validator

from .base import NonBlankText, StrictContract, StrictPositiveInt
from .content import Asset, Topic
from .enums import IndexDecision, JsonIndexDecision, JsonPageType
from .identifiers import asset_id, assign_asset_ids, assign_topic_ids, topic_id
from .model_response import PageOutput


class PageArtifact(StrictContract):
    """Exact eight-field JSON contract consumed by the future indexer."""

    page_number: StrictPositiveInt
    page_type: JsonPageType
    page_image_path: NonBlankText
    index_decision: JsonIndexDecision
    index_reason: NonBlankText
    summary: str
    topics: list[Topic]
    assets: list[Asset]

    @model_validator(mode="after")
    def validate_contract(self) -> PageArtifact:
        expected_image = f"page_images/page-{self.page_number:04d}.png"
        if self.page_image_path != expected_image:
            raise ValueError(f"page_image_path must be {expected_image!r}")
        if self.index_decision is IndexDecision.NON_INDEX_WORTHY:
            if self.summary != "" or self.topics or self.assets:
                raise ValueError("non-index-worthy pages require empty summary, topics, and assets")
        elif not self.summary or self.summary.isspace():
            raise ValueError("index-worthy and mixed pages require a meaningful summary")

        expected_topics = [
            topic_id(self.page_number, ordinal) for ordinal in range(1, len(self.topics) + 1)
        ]
        if [topic.topic_id for topic in self.topics] != expected_topics:
            raise ValueError("topic identifiers must match page number and list order")

        expected_assets = [
            asset_id(self.page_number, ordinal) for ordinal in range(1, len(self.assets) + 1)
        ]
        if [asset.asset_id for asset in self.assets] != expected_assets:
            raise ValueError("asset identifiers must match page number and list order")
        return self


def build_page_artifact(
    page_output: PageOutput,
    *,
    page_number: int,
    page_image_path: str,
) -> PageArtifact:
    """Attach only system-owned fields to a validated model page output."""

    return PageArtifact(
        page_number=page_number,
        page_type=page_output.page_type,
        page_image_path=page_image_path,
        index_decision=IndexDecision(page_output.index_decision),
        index_reason=page_output.index_reason,
        summary=page_output.summary,
        topics=assign_topic_ids(page_number, page_output.topics),
        assets=assign_asset_ids(page_number, page_output.assets),
    )
