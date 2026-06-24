from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from uuid import UUID
from pydantic import BaseModel
from typing import Optional
from app.core.database import get_db
from app.api.deps import AdminOnly, AnyAuthenticated, AnyStaff
from app.utils.response import success_response

router = APIRouter()

class CreateArticleRequest(BaseModel):
    title: str; content: str; category: Optional[str] = None; tags: Optional[str] = None

class UpdateArticleRequest(BaseModel):
    title: Optional[str] = None; content: Optional[str] = None; category: Optional[str] = None; is_published: Optional[bool] = None

@router.get("/articles", summary="List knowledge base articles")
async def list_articles(category: str = Query(None), page: int = Query(1, ge=1), per_page: int = Query(20), db: AsyncSession = Depends(get_db)):
    from app.models.knowledge_base import KBArticle
    q = select(KBArticle).where(KBArticle.is_active == True, KBArticle.is_published == True)
    if category: q = q.where(KBArticle.category == category)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    articles = (await db.execute(q.offset((page-1)*per_page).limit(per_page))).scalars().all()
    return success_response(data={"items": [{"id": str(a.id), "title": a.title, "category": a.category, "tags": a.tags, "created_at": a.created_at.isoformat()} for a in articles], "total": total})

@router.post("/articles", summary="Create article [Admin]")
async def create_article(payload: CreateArticleRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.knowledge_base import KBArticle
    article = KBArticle(title=payload.title, content=payload.content, category=payload.category, tags=payload.tags, created_by=UUID(current_user["user_id"]), is_published=True)
    db.add(article); await db.commit()
    return success_response(data={"id": str(article.id)}, message="Article created")

@router.put("/articles/{article_id}", summary="Update article [Admin]")
async def update_article(article_id: UUID, payload: UpdateArticleRequest, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.knowledge_base import KBArticle
    article = (await db.execute(select(KBArticle).where(KBArticle.id == article_id))).scalar_one_or_none()
    if not article: raise HTTPException(status_code=404, detail="Article not found")
    for f, v in payload.model_dump(exclude_none=True).items(): setattr(article, f, v)
    await db.commit()
    return success_response(message="Article updated")

@router.delete("/articles/{article_id}", summary="Delete article [Admin]")
async def delete_article(article_id: UUID, current_user: dict = Depends(AdminOnly), db: AsyncSession = Depends(get_db)):
    from app.models.knowledge_base import KBArticle
    article = (await db.execute(select(KBArticle).where(KBArticle.id == article_id))).scalar_one_or_none()
    if not article: raise HTTPException(status_code=404, detail="Article not found")
    article.is_active = False; await db.commit()
    return success_response(message="Article deleted")

@router.get("/videos", summary="List knowledge base videos")
async def list_videos(db: AsyncSession = Depends(get_db)):
    from app.models.knowledge_base import KBVideo
    videos = (await db.execute(select(KBVideo).where(KBVideo.is_active == True))).scalars().all()
    return success_response(data=[{"id": str(v.id), "title": v.title, "url": v.url, "category": v.category, "duration_seconds": v.duration_seconds} for v in videos])

@router.get("/search", summary="Search knowledge base")
async def search_kb(q: str = Query(..., min_length=2), db: AsyncSession = Depends(get_db)):
    from app.models.knowledge_base import KBArticle
    results = (await db.execute(select(KBArticle).where(KBArticle.is_active == True, KBArticle.is_published == True, or_(KBArticle.title.ilike(f"%{q}%"), KBArticle.content.ilike(f"%{q}%"), KBArticle.tags.ilike(f"%{q}%"))))).scalars().all()
    return success_response(data=[{"id": str(a.id), "title": a.title, "category": a.category} for a in results])
