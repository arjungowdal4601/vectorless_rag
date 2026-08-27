"""Offline tests for final page artifacts and stable identifiers."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from document_processing.contracts import (
    AssetDraft,
    IndexWorthyPageOutput,
    NonIndexWorthyPageOutput,
    PageArtifact,
    PageType,
    TopicDraft,
    assign_asset_ids,
    assign_topic_ids,
    build_page_artifact,
)


class ArtifactTests(unittest.TestCase):
    def test_stable_ids_use_page_and_one_based_order(self) -> None:
        topics = [
            TopicDraft(topic_name="Same", topic_description="First meaning."),
            TopicDraft(topic_name="Same", topic_description="Second meaning."),
        ]
        assets = [
            AssetDraft(
                asset_type="table",
                asset_name="",
                asset_description="Visible values.",
            ),
            AssetDraft(
                asset_type="table",
                asset_name="",
                asset_description="More visible values.",
            ),
        ]

        self.assertEqual(
            [topic.topic_id for topic in assign_topic_ids(1, topics)],
            ["p0001-t001", "p0001-t002"],
        )
        self.assertEqual(
            [asset.asset_id for asset in assign_asset_ids(12, assets)],
            ["p0012-a001", "p0012-a002"],
        )

    def test_builds_exact_eight_field_page_json(self) -> None:
        output = IndexWorthyPageOutput(
            page_type=PageType.BODY_CONTENT,
            index_decision="index_worthy",
            index_reason="Contains a substantive requirement.",
            summary="Explains the visible requirement.",
            topics=[
                TopicDraft(
                    topic_name="Requirement",
                    topic_description="The threshold is 5 mg.",
                )
            ],
            assets=[],
        )
        artifact = build_page_artifact(
            output,
            page_number=3,
            page_image_path="page_images/page-0003.png",
        )

        dumped = artifact.model_dump(mode="json")
        self.assertEqual(
            list(dumped),
            [
                "page_number",
                "page_type",
                "page_image_path",
                "index_decision",
                "index_reason",
                "summary",
                "topics",
                "assets",
            ],
        )
        self.assertEqual(dumped["topics"][0]["topic_id"], "p0003-t001")

    def test_non_index_worthy_page_has_empty_content(self) -> None:
        output = NonIndexWorthyPageOutput(
            page_type=PageType.TITLE,
            index_decision="non_index_worthy",
            index_reason="The page contains title metadata only.",
            summary="",
            topics=[],
            assets=[],
        )
        artifact = build_page_artifact(
            output,
            page_number=1,
            page_image_path="page_images/page-0001.png",
        )
        self.assertEqual(artifact.summary, "")
        self.assertEqual(artifact.topics, [])
        self.assertEqual(artifact.assets, [])

    def test_rejects_wrong_or_model_supplied_identifier(self) -> None:
        output = IndexWorthyPageOutput(
            page_type=PageType.BODY_CONTENT,
            index_decision="index_worthy",
            index_reason="Substantive.",
            summary="Visible content.",
            topics=[TopicDraft(topic_name="X", topic_description="Y")],
            assets=[],
        )
        valid = build_page_artifact(
            output,
            page_number=1,
            page_image_path="page_images/page-0001.png",
        ).model_dump(mode="json")
        valid["topics"][0]["topic_id"] = "p0001-t999"
        with self.assertRaises(ValidationError):
            PageArtifact.model_validate(valid)

    def test_persisted_enums_do_not_coerce_bytes(self) -> None:
        output = NonIndexWorthyPageOutput(
            page_type=PageType.TITLE,
            index_decision="non_index_worthy",
            index_reason="Visible title metadata.",
            summary="",
            topics=[],
            assets=[],
        )
        payload = build_page_artifact(
            output,
            page_number=1,
            page_image_path="page_images/page-0001.png",
        ).model_dump(mode="json")
        for field in ("page_type", "index_decision"):
            invalid = dict(payload)
            invalid[field] = str(payload[field]).encode()
            with self.subTest(field=field), self.assertRaises(ValidationError):
                PageArtifact.model_validate(invalid)

    def test_page_image_path_is_stable_and_page_bound(self) -> None:
        output = NonIndexWorthyPageOutput(
            page_type=PageType.BLANK,
            index_decision="non_index_worthy",
            index_reason="The visible page is blank.",
            summary="",
            topics=[],
            assets=[],
        )
        with self.assertRaises(ValidationError):
            build_page_artifact(
                output,
                page_number=2,
                page_image_path="page_images/page-0001.png",
            )

        valid = build_page_artifact(
            output,
            page_number=1,
            page_image_path="page_images/page-0001.png",
        ).model_dump(mode="json")
        valid["schema_version"] = "unexpected"
        with self.assertRaises(ValidationError):
            PageArtifact.model_validate(valid)

    def test_rejects_boolean_page_number(self) -> None:
        with self.assertRaises(TypeError):
            assign_topic_ids(
                True,
                [TopicDraft(topic_name="X", topic_description="Y")],
            )


if __name__ == "__main__":
    unittest.main()
