"""Offline tests for the native model response contract."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from document_processing.contracts import ModelResponse


def active_position() -> dict[str, object]:
    return {
        "parent_section": ["3 Safety"],
        "current_subsection": "3.2 Limits",
        "last_visible_clause": "The dose must not exceed 5 mg.",
        "current_topic_flow": "Dose limits",
        "unfinished_content": None,
        "next_page_inspection": None,
        "document_completion": "In progress",
    }


def response_payload(
    *,
    decision: str = "index_worthy",
    summary: str = "Explains the visible dose limit.",
    topics: list[dict[str, object]] | None = None,
    assets: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "memory_edits": {
            "short_term_memory_edits": [
                {
                    "edit_type": "append_new_section",
                    "section_heading": "Active Reading Position",
                    "content": active_position(),
                }
            ],
            "document_structure_edits": {"topics": ["3.2 Limits"]},
        },
        "page_output": {
            "page_type": "body_content",
            "index_decision": decision,
            "index_reason": "The page contains a substantive requirement.",
            "summary": summary,
            "topics": topics
            if topics is not None
            else [
                {
                    "topic_name": "Dose limit",
                    "topic_description": "The dose must not exceed 5 mg.",
                }
            ],
            "assets": assets if assets is not None else [],
        },
    }


class ModelResponseTests(unittest.TestCase):
    def test_accepts_exact_index_worthy_contract(self) -> None:
        response = ModelResponse.model_validate(response_payload())

        dumped = response.model_dump(mode="json", by_alias=True)
        self.assertEqual(set(dumped), {"memory_edits", "page_output"})
        self.assertEqual(
            set(dumped["page_output"]),
            {
                "page_type",
                "index_decision",
                "index_reason",
                "summary",
                "topics",
                "assets",
            },
        )

    def test_non_index_worthy_requires_exact_empty_payload(self) -> None:
        valid = response_payload(
            decision="non_index_worthy",
            summary="",
            topics=[],
            assets=[],
        )
        ModelResponse.model_validate(valid)

        for field, value in (
            ("summary", "Not empty"),
            ("topics", [{"topic_name": "X", "topic_description": "Y"}]),
            (
                "assets",
                [
                    {
                        "asset_type": "table",
                        "asset_name": "",
                        "asset_description": "Visible values.",
                    }
                ],
            ),
        ):
            invalid = response_payload(
                decision="non_index_worthy",
                summary="",
                topics=[],
                assets=[],
            )
            invalid["page_output"][field] = value  # type: ignore[index]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ModelResponse.model_validate(invalid)

    def test_indexable_summary_must_not_be_blank(self) -> None:
        for decision in ("index_worthy", "mixed"):
            for summary in ("", "   "):
                with (
                    self.subTest(decision=decision, summary=summary),
                    self.assertRaises(ValidationError),
                ):
                    ModelResponse.model_validate(
                        response_payload(
                            decision=decision,
                            summary=summary,
                            topics=[],
                        )
                    )

    def test_indexable_topics_and_assets_may_be_empty(self) -> None:
        ModelResponse.model_validate(response_payload(decision="mixed", topics=[], assets=[]))

    def test_rejects_system_owned_fields_at_every_level(self) -> None:
        attacks = (
            ("page_number", 999),
            ("page_image_path", "../../escape.png"),
        )
        for field, value in attacks:
            payload = response_payload()
            payload["page_output"][field] = value  # type: ignore[index]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ModelResponse.model_validate(payload)

        for field in ("topic_id", "asset_id"):
            payload = response_payload(
                assets=[
                    {
                        "asset_type": "table",
                        "asset_name": "",
                        "asset_description": "Visible values.",
                    }
                ]
            )
            target = "topics" if field == "topic_id" else "assets"
            payload["page_output"][target][0][field] = "attacker-owned"  # type: ignore[index]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ModelResponse.model_validate(payload)

    def test_requires_exactly_one_memory_edit(self) -> None:
        for edits in ([], [response_payload()["memory_edits"]] * 2):
            payload = response_payload()
            if edits:
                one = payload["memory_edits"]["short_term_memory_edits"][0]  # type: ignore[index]
                value = [one, one]
            else:
                value = []
            payload["memory_edits"]["short_term_memory_edits"] = value  # type: ignore[index]
            with self.assertRaises(ValidationError):
                ModelResponse.model_validate(payload)

    def test_asset_name_may_be_empty_but_not_whitespace(self) -> None:
        payload = response_payload(
            assets=[
                {
                    "asset_type": "diagram",
                    "asset_name": "",
                    "asset_description": "Shows the visible process.",
                }
            ]
        )
        ModelResponse.model_validate(payload)
        payload["page_output"]["assets"][0]["asset_name"] = "   "  # type: ignore[index]
        with self.assertRaises(ValidationError):
            ModelResponse.model_validate(payload)

    def test_rejects_wrong_enum_casing_and_numeric_types(self) -> None:
        for field, value in (
            ("page_type", "Body_Content"),
            ("page_type", 1),
            ("page_type", b"body_content"),
            ("index_decision", "INDEX_WORTHY"),
        ):
            payload = response_payload()
            payload["page_output"][field] = value  # type: ignore[index]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ModelResponse.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
