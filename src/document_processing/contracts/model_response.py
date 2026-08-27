"""Native structured-output contract for the multimodal page model."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .base import NonBlankText, StrictContract
from .content import AssetDraft, TopicDraft
from .enums import JsonPageType
from .memory import ShortTermMemoryEdit
from .structure import DocumentStructureEdit


class _PageOutputBase(StrictContract):
    page_type: JsonPageType
    index_decision: str
    index_reason: NonBlankText
    summary: str
    topics: list[TopicDraft]
    assets: list[AssetDraft]


class NonIndexWorthyPageOutput(_PageOutputBase):
    index_decision: Literal["non_index_worthy"]
    summary: Literal[""]
    topics: list[TopicDraft] = Field(max_length=0)
    assets: list[AssetDraft] = Field(max_length=0)


class IndexWorthyPageOutput(_PageOutputBase):
    index_decision: Literal["index_worthy"]
    summary: NonBlankText


class MixedPageOutput(_PageOutputBase):
    index_decision: Literal["mixed"]
    summary: NonBlankText


type PageOutput = Annotated[
    NonIndexWorthyPageOutput | IndexWorthyPageOutput | MixedPageOutput,
    Field(discriminator="index_decision"),
]


class MemoryEdits(StrictContract):
    short_term_memory_edits: list[ShortTermMemoryEdit] = Field(
        min_length=1,
        max_length=1,
    )
    document_structure_edits: DocumentStructureEdit


class ModelResponse(StrictContract):
    """The two-section response consumed from native structured output."""

    memory_edits: MemoryEdits
    page_output: PageOutput
