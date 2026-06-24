from pydantic import BaseModel
from typing import Any, Optional, List, TypeVar, Generic

T = TypeVar("T")

class SuccessResponse(BaseModel):
    success: bool = True
    message: str = "Success"
    data: Optional[Any] = None

class ErrorResponse(BaseModel):
    success: bool = False
    message: str
    errors: Optional[Any] = None

class PaginatedData(BaseModel, Generic[T]):
    items: List[Any]
    total: int
    page: int
    per_page: int
    pages: int
