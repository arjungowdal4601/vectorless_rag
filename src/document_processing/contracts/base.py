"""Shared strict types for document-processing contracts."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic.functional_validators import AfterValidator


class StrictContract(BaseModel):
    """Base class that rejects coercion, unknown fields, and mutation."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_default=True,
    )


def _empty_or_nonblank(value: str) -> str:
    if value and value.isspace():
        raise ValueError("value must be empty or contain a non-whitespace character")
    return value


type NonBlankText = Annotated[str, StringConstraints(pattern=r"\S")]
type EmptyOrNonBlankText = Annotated[str, AfterValidator(_empty_or_nonblank)]
type StrictPositiveInt = Annotated[int, Field(strict=True, ge=1)]
type StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
type Sha256Hex = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]
