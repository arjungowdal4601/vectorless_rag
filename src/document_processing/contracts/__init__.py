"""Public domain contracts for the document-processing layer."""

from .artifacts import PageArtifact, build_page_artifact
from .content import Asset, AssetDraft, Topic, TopicDraft
from .enums import (
    DocumentCompletion,
    ErrorCategory,
    IndexDecision,
    MemoryEditType,
    PageStatus,
    PageType,
    RunStatus,
)
from .identifiers import asset_id, assign_asset_ids, assign_topic_ids, topic_id
from .memory import (
    ACTIVE_READING_POSITION,
    ActiveReadingPosition,
    AppendNewSectionMemoryEdit,
    AppendToSectionMemoryEdit,
    EmptyShortTermMemory,
    NoChangeMemoryEdit,
    ReplaceSectionMemoryEdit,
    ShortTermMemory,
    ShortTermMemoryEdit,
    ShortTermMemoryState,
)
from .model_response import (
    IndexWorthyPageOutput,
    MemoryEdits,
    MixedPageOutput,
    ModelResponse,
    NonIndexWorthyPageOutput,
    PageOutput,
)
from .processing import (
    ModelUsageRecord,
    PageProcessingState,
    ProcessingError,
    ProcessingManifest,
)
from .structure import (
    DocumentStructure,
    DocumentStructureEdit,
    DocumentStructurePage,
)
from .transitions import (
    ContractTransitionError,
    append_document_structure_page,
    apply_short_term_memory_edit,
)

__all__ = [
    "ACTIVE_READING_POSITION",
    "ActiveReadingPosition",
    "AppendNewSectionMemoryEdit",
    "AppendToSectionMemoryEdit",
    "Asset",
    "AssetDraft",
    "ContractTransitionError",
    "DocumentCompletion",
    "DocumentStructure",
    "DocumentStructureEdit",
    "DocumentStructurePage",
    "EmptyShortTermMemory",
    "ErrorCategory",
    "IndexDecision",
    "IndexWorthyPageOutput",
    "MemoryEditType",
    "MemoryEdits",
    "MixedPageOutput",
    "ModelResponse",
    "ModelUsageRecord",
    "NoChangeMemoryEdit",
    "NonIndexWorthyPageOutput",
    "PageArtifact",
    "PageOutput",
    "PageProcessingState",
    "PageStatus",
    "PageType",
    "ProcessingError",
    "ProcessingManifest",
    "ReplaceSectionMemoryEdit",
    "RunStatus",
    "ShortTermMemory",
    "ShortTermMemoryEdit",
    "ShortTermMemoryState",
    "Topic",
    "TopicDraft",
    "append_document_structure_page",
    "apply_short_term_memory_edit",
    "asset_id",
    "assign_asset_ids",
    "assign_topic_ids",
    "build_page_artifact",
    "topic_id",
]
