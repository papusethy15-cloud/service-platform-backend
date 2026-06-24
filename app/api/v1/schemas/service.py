from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class CreateServiceCategoryRequest(BaseModel):
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    sort_order: int = 0

class UpdateServiceCategoryRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    sort_order: Optional[int] = None

class CreateServiceRequest(BaseModel):
    category_id: str
    name: str
    description: Optional[str] = None
    base_price: float
    gst_percent: float = 18.0
    duration_mins: int = 60
    is_visible: bool = True
    sort_order: int = 0

class UpdateServiceRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    base_price: Optional[float] = None
    gst_percent: Optional[float] = None
    duration_mins: Optional[int] = None
    is_visible: Optional[bool] = None

class ServiceCategoryResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    icon: Optional[str]
    sort_order: int
    is_active: bool
    created_at: datetime
    class Config:
        from_attributes = True

class ServiceResponse(BaseModel):
    id: str
    category_id: str
    name: str
    description: Optional[str]
    base_price: float
    gst_percent: float
    duration_mins: int
    is_visible: bool
    sort_order: int
    created_at: datetime
    class Config:
        from_attributes = True
