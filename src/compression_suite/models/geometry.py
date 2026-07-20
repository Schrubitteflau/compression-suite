"""Shared pixel geometry models."""

from pydantic import BaseModel, Field


class PixelRectangle(BaseModel):
    """An axis-aligned rectangle expressed in source-image pixels."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @property
    def right(self) -> int:
        """Exclusive horizontal edge."""
        return self.x + self.width

    @property
    def bottom(self) -> int:
        """Exclusive vertical edge."""
        return self.y + self.height
