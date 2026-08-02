from celery_app import celery_app
import reservation_recovery


@celery_app.task(name="celery_tasks.sync_flight_seat_cache")
def sync_flight_seat_cache():
    return reservation_recovery.recover_reservation_state()
