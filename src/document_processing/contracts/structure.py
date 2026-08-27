"""Page-ordered document-navigation artifacts."""

from __future__ import annotations

from pydantic import model_validator

from .base import NonBlankText, StrictContract, StrictPositiveInt


def _reject_duplicate_topics(topics: list[str]) -> None:
    if len(topics) != len(set(topics)):
        raise ValueError("document-structure topics must be unique within a page")


class DocumentStructureEdit(StrictContract):
    """Navigation observations authored for the current page."""

    topics: list[NonBlankText]

    @model_validator(mode="after")
    def validate_unique_topics(self) -> DocumentStructureEdit:
        _reject_duplicate_topics(self.topics)
        return self


class DocumentStructurePage(StrictContract):
    page_number: StrictPositiveInt
    topics: list[NonBlankText]

    @model_validator(mode="after")
    def validate_unique_topics(self) -> DocumentStructurePage:
        _reject_duplicate_topics(self.topics)
        return self


class DocumentStructure(StrictContract):
    """Final structure envelope, including an entry for every page."""

    pages: list[DocumentStructurePage]

    @model_validator(mode="after")
    def validate_page_order(self) -> DocumentStructure:
        actual = [page.page_number for page in self.pages]
        expected = list(range(1, len(self.pages) + 1))
        if actual != expected:
            raise ValueError("document-structure pages must be contiguous and ordered")
        return self
