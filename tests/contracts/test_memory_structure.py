"""Offline tests for memory and document-structure reducers."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from document_processing.contracts import (
    ACTIVE_READING_POSITION,
    ActiveReadingPosition,
    AppendNewSectionMemoryEdit,
    AppendToSectionMemoryEdit,
    ContractTransitionError,
    DocumentCompletion,
    DocumentStructure,
    EmptyShortTermMemory,
    NoChangeMemoryEdit,
    ReplaceSectionMemoryEdit,
    ShortTermMemory,
    append_document_structure_page,
    apply_short_term_memory_edit,
)


def position(*, subsection: str = "1 Scope") -> ActiveReadingPosition:
    return ActiveReadingPosition(
        parent_section=None,
        current_subsection=subsection,
        last_visible_clause=None,
        current_topic_flow="Document scope",
        unfinished_content=None,
        next_page_inspection=None,
        document_completion=DocumentCompletion.IN_PROGRESS,
    )


class MemoryTests(unittest.TestCase):
    def test_document_completion_does_not_coerce_bytes(self) -> None:
        payload = position().model_dump(mode="json")
        payload["document_completion"] = b"In progress"
        with self.assertRaises(ValidationError):
            ActiveReadingPosition.model_validate(payload)

    def test_initial_and_populated_envelopes_are_exact(self) -> None:
        self.assertEqual(EmptyShortTermMemory().model_dump(mode="json"), {})
        memory = ShortTermMemory.model_validate({ACTIVE_READING_POSITION: position()})
        dumped = memory.model_dump(mode="json", by_alias=True)
        self.assertEqual(set(dumped), {ACTIVE_READING_POSITION})
        self.assertEqual(
            set(dumped[ACTIVE_READING_POSITION]),
            {
                "parent_section",
                "current_subsection",
                "last_visible_clause",
                "current_topic_flow",
                "unfinished_content",
                "next_page_inspection",
                "document_completion",
            },
        )

    def test_first_page_creates_and_later_page_replaces(self) -> None:
        first_edit = AppendNewSectionMemoryEdit(
            edit_type="append_new_section",
            section_heading=ACTIVE_READING_POSITION,
            content=position(subsection="1 Scope"),
        )
        first = apply_short_term_memory_edit(
            EmptyShortTermMemory(),
            first_edit,
            page_number=1,
        )
        second_edit = ReplaceSectionMemoryEdit(
            edit_type="replace_section",
            section_heading=ACTIVE_READING_POSITION,
            content=position(subsection="2 Requirements"),
        )
        second = apply_short_term_memory_edit(first, second_edit, page_number=2)

        self.assertEqual(
            second.active_reading_position.current_subsection,
            "2 Requirements",
        )
        self.assertNotIn("1 Scope", second.model_dump_json(by_alias=True))

    def test_other_allowed_edit_types_are_illegal_transitions(self) -> None:
        memory = ShortTermMemory.model_validate({ACTIVE_READING_POSITION: position()})
        no_change = NoChangeMemoryEdit(
            edit_type="no_change",
            section_heading=ACTIVE_READING_POSITION,
            content=None,
        )
        append = AppendToSectionMemoryEdit(
            edit_type="append_to_section",
            section_heading=ACTIVE_READING_POSITION,
            content=position(),
        )
        for edit in (no_change, append):
            with self.subTest(edit=edit.edit_type), self.assertRaises(ContractTransitionError):
                apply_short_term_memory_edit(memory, edit, page_number=2)

    def test_rejects_wrong_phase_memory_state(self) -> None:
        replace = ReplaceSectionMemoryEdit(
            edit_type="replace_section",
            section_heading=ACTIVE_READING_POSITION,
            content=position(),
        )
        with self.assertRaises(ContractTransitionError):
            apply_short_term_memory_edit(
                EmptyShortTermMemory(),
                replace,
                page_number=2,
            )

    def test_none_is_json_null_and_string_none_is_rejected(self) -> None:
        raw = position().model_dump(mode="json")
        self.assertIsNone(raw["parent_section"])
        raw["parent_section"] = "None"
        with self.assertRaises(ValidationError):
            ActiveReadingPosition.model_validate(raw)

    def test_empty_parent_hierarchy_is_rejected(self) -> None:
        raw = position().model_dump(mode="json")
        raw["parent_section"] = []
        with self.assertRaises(ValidationError):
            ActiveReadingPosition.model_validate(raw)


class DocumentStructureTests(unittest.TestCase):
    def test_appends_pages_contiguously_and_preserves_order(self) -> None:
        structure = DocumentStructure(pages=[])
        structure = append_document_structure_page(
            structure,
            page_number=1,
            topics=["2 Procedure", "2.1 Inputs"],
        )
        structure = append_document_structure_page(
            structure,
            page_number=2,
            topics=[],
        )
        self.assertEqual(
            structure.model_dump(mode="json"),
            {
                "pages": [
                    {
                        "page_number": 1,
                        "topics": ["2 Procedure", "2.1 Inputs"],
                    },
                    {"page_number": 2, "topics": []},
                ]
            },
        )

    def test_rejects_gap_reorder_and_duplicate_topics(self) -> None:
        with self.assertRaises(ContractTransitionError):
            append_document_structure_page(
                DocumentStructure(pages=[]),
                page_number=2,
                topics=["Heading"],
            )
        with self.assertRaises(ValidationError):
            append_document_structure_page(
                DocumentStructure(pages=[]),
                page_number=1,
                topics=["Heading", "Heading"],
            )


if __name__ == "__main__":
    unittest.main()
