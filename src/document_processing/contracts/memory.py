"""Short-term-memory artifacts and model-authored edit operations."""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import ConfigDict, Field, field_validator

from .base import NonBlankText, StrictContract
from .enums import JsonDocumentCompletion

ACTIVE_READING_POSITION: Final[Literal["Active Reading Position"]] = "Active Reading Position"


class ActiveReadingPosition(StrictContract):
    """The only contextual state allowed to flow to the next page."""

    parent_section: list[NonBlankText] | None
    current_subsection: NonBlankText | None
    last_visible_clause: NonBlankText | None
    current_topic_flow: NonBlankText | None
    unfinished_content: NonBlankText | None
    next_page_inspection: NonBlankText | None
    document_completion: JsonDocumentCompletion

    @field_validator("parent_section")
    @classmethod
    def reject_empty_parent_hierarchy(
        cls,
        value: list[str] | None,
    ) -> list[str] | None:
        if value == []:
            raise ValueError("use null when no parent section applies")
        return value


class EmptyShortTermMemory(StrictContract):
    """Initial memory artifact before page one commits."""


class ShortTermMemory(StrictContract):
    """Post-page memory with exactly one named section."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_default=True,
        validate_by_alias=True,
        validate_by_name=False,
        serialize_by_alias=True,
    )

    active_reading_position: ActiveReadingPosition = Field(
        alias="Active Reading Position",
    )


type ShortTermMemoryState = EmptyShortTermMemory | ShortTermMemory


class NoChangeMemoryEdit(StrictContract):
    edit_type: Literal["no_change"]
    section_heading: Literal["Active Reading Position"]
    content: None


class AppendNewSectionMemoryEdit(StrictContract):
    edit_type: Literal["append_new_section"]
    section_heading: Literal["Active Reading Position"]
    content: ActiveReadingPosition


class AppendToSectionMemoryEdit(StrictContract):
    edit_type: Literal["append_to_section"]
    section_heading: Literal["Active Reading Position"]
    content: ActiveReadingPosition


class ReplaceSectionMemoryEdit(StrictContract):
    edit_type: Literal["replace_section"]
    section_heading: Literal["Active Reading Position"]
    content: ActiveReadingPosition


type ShortTermMemoryEdit = Annotated[
    NoChangeMemoryEdit
    | AppendNewSectionMemoryEdit
    | AppendToSectionMemoryEdit
    | ReplaceSectionMemoryEdit,
    Field(discriminator="edit_type"),
]
