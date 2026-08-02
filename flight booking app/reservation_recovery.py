from datetime import datetime

import dbcon
import redis_seats


def remaining_ttl_seconds(expires_at, now):
    if not expires_at:
        return 0
    return max(int((expires_at - now).total_seconds()), 0)


def recover_reservation_state(db=None):
    owns_session = db is None
    db = db or dbcon.SESSION_LOCAL()

    restored = 0
    expired = 0
    synced_instances = set()

    try:
        now = datetime.utcnow()

        pending_bookings = db.query(dbcon.Booking).filter(
            dbcon.Booking.status == "pending"
        ).all()

        for booking in pending_bookings:
            ttl = remaining_ttl_seconds(booking.reservation_expires_at, now)

            if ttl <= 0:
                if transition_pending_status(db, booking, "expired"):
                    redis_seats.release_hold(booking.idempotency_key, return_seats=True)
                    expired += 1
                    synced_instances.add(booking.flight_instance_id)
                continue

            seat_record = db.query(dbcon.FlightSeat).filter(
                dbcon.FlightSeat.flight_instance_id == booking.flight_instance_id
            ).first()

            if not seat_record:
                continue

            current_hold = redis_seats.get_hold(booking.idempotency_key)
            if not current_hold:
                redis_seats.restore_hold(seat_record, booking, ttl)
                restored += 1

            redis_seats.sync_flight_cache(seat_record)
            synced_instances.add(booking.flight_instance_id)

        for seat_record in db.query(dbcon.FlightSeat).all():
            redis_seats.sync_flight_cache(seat_record)

        db.commit()

        for flight_instance_id in synced_instances:
            seat_record = db.query(dbcon.FlightSeat).filter(
                dbcon.FlightSeat.flight_instance_id == flight_instance_id
            ).first()
            if seat_record:
                availability = redis_seats.cached_availability(seat_record)
                redis_seats.publish_seat_update(flight_instance_id, availability)

        return {
            "restored_holds": restored,
            "expired_bookings": expired,
            "synced_flights": db.query(dbcon.FlightSeat).count(),
        }
    finally:
        if owns_session:
            db.close()


def recover_booking_hold(db, booking):
    if booking.status != "pending":
        return False

    now = datetime.utcnow()
    ttl = remaining_ttl_seconds(booking.reservation_expires_at, now)

    if ttl <= 0:
        if transition_pending_status(db, booking, "expired"):
            db.commit()
            redis_seats.release_hold(booking.idempotency_key, return_seats=True)
        return False

    seat_record = db.query(dbcon.FlightSeat).filter(
        dbcon.FlightSeat.flight_instance_id == booking.flight_instance_id
    ).first()

    if not seat_record:
        return False

    redis_seats.restore_hold(seat_record, booking, ttl)
    availability = redis_seats.sync_flight_cache(seat_record)
    redis_seats.publish_seat_update(booking.flight_instance_id, availability)
    return True


def transition_pending_status(db, booking, new_status):
    updated_booking = db.query(dbcon.Booking).filter(
        dbcon.Booking.id == booking.id,
        dbcon.Booking.status == "pending"
    ).update(
        {dbcon.Booking.status: new_status},
        synchronize_session=False
    )

    if updated_booking == 0:
        db.rollback()
        db.refresh(booking)
        return False

    booking.status = new_status
    return True
