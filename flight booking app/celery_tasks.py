from celery_app import celery_app
import reservation_recovery
from recommender_service import train_implicit_model


@celery_app.task(name="celery_tasks.sync_flight_seat_cache")
def sync_flight_seat_cache():
    return reservation_recovery.recover_reservation_state()


@celery_app.task(name="celery_tasks.train_flight_recommender")
def train_flight_recommender():
    return train_implicit_model()
