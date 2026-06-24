from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "palei_solutions",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.task_routes = {
    "app.tasks.notifications.*": {"queue": "notifications"},
    "app.tasks.reports.*":       {"queue": "reports"},
}
