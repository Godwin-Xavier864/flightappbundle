import os

from celery import Celery


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "flight_booking",
    broker=os.getenv("CELERY_BROKER_URL", REDIS_URL),
    backend=os.getenv("CELERY_RESULT_BACKEND", REDIS_URL),
    include=["celery_tasks"],
)

celery_app.conf.task_default_queue = "flight_booking"
celery_app.conf.task_routes = {
    "celery_tasks.*": {"queue": "flight_booking"},
}
celery_app.conf.worker_pool = os.getenv("CELERY_WORKER_POOL", "solo")
celery_app.conf.beat_schedule = {
    "sync-flight-seat-cache-every-minute": {
        "task": "celery_tasks.sync_flight_seat_cache",
        "options": {"queue": "flight_booking"},
        "schedule": 60.0,
    },
    "train-flight-recommender-every-10-minutes": {
        "task": "celery_tasks.train_flight_recommender",
        "options": {"queue": "flight_booking"},
        "schedule": 600.0,
    },
}
celery_app.conf.timezone = "UTC"
