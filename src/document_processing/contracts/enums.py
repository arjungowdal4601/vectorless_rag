"""Closed vocabularies fixed by the document-processing contract."""

from enum import StrEnum
from typing import Annotated

from pydantic import BeforeValidator, Field


def _require_enum_string(value: object) -> object:
    """Accept JSON strings or an already-built enum, never coercible values."""

    if not isinstance(value, str):
        raise ValueError("enum values must be JSON strings")
    return value


class PageType(StrEnum):
    TITLE = "title"
    AUTHORS = "authors"
    ABSTRACT = "abstract"
    TABLE_OF_CONTENTS = "table_of_contents"
    FIGURE_TABLE_LIST = "figure_table_list"
    GLOSSARY = "glossary"
    ABBREVIATIONS = "abbreviations"
    BODY_CONTENT = "body_content"
    APPENDIX_CONTENT = "appendix_content"
    REFERENCES = "references"
    LEGAL_ADMIN = "legal_admin"
    BLANK = "blank"
    UNKNOWN = "unknown"


class IndexDecision(StrEnum):
    INDEX_WORTHY = "index_worthy"
    NON_INDEX_WORTHY = "non_index_worthy"
    MIXED = "mixed"


class MemoryEditType(StrEnum):
    NO_CHANGE = "no_change"
    APPEND_NEW_SECTION = "append_new_section"
    APPEND_TO_SECTION = "append_to_section"
    REPLACE_SECTION = "replace_section"


class DocumentCompletion(StrEnum):
    COMPLETE = "Complete"
    IN_PROGRESS = "In progress"


class PageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RESUMING = "resuming"


class ErrorCategory(StrEnum):
    INPUT = "input"
    RENDERING = "rendering"
    TRANSIENT_MODEL = "transient_model"
    PERMANENT_MODEL = "permanent_model"
    STORAGE = "storage"
    INTEGRITY = "integrity"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


type JsonPageType = Annotated[
    PageType,
    Field(strict=False),
    BeforeValidator(_require_enum_string),
]
type JsonIndexDecision = Annotated[
    IndexDecision,
    Field(strict=False),
    BeforeValidator(_require_enum_string),
]
type JsonDocumentCompletion = Annotated[
    DocumentCompletion,
    Field(strict=False),
    BeforeValidator(_require_enum_string),
]
type JsonPageStatus = Annotated[
    PageStatus,
    Field(strict=False),
    BeforeValidator(_require_enum_string),
]
type JsonRunStatus = Annotated[
    RunStatus,
    Field(strict=False),
    BeforeValidator(_require_enum_string),
]
type JsonErrorCategory = Annotated[
    ErrorCategory,
    Field(strict=False),
    BeforeValidator(_require_enum_string),
]
