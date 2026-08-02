import os
import random
import uuid


DUMMY_PAYMENT_SUCCESS_RATE = float(os.getenv("DUMMY_PAYMENT_SUCCESS_RATE", "0.8"))


def new_payment_order_id():
    return f"pay_dummy_{uuid.uuid4().hex[:16]}"


def create_dummy_payment_session(amount, booking, idempotency_key):
    return {
        "id": booking.payment_order_id,
        "provider": "dummy",
        "status": "created",
        "amount": amount * 100,
        "amount_rupees": amount,
        "currency": "INR",
        "idempotency_key": idempotency_key,
        "expires_at": (
            booking.reservation_expires_at.isoformat()
            if booking.reservation_expires_at
            else None
        ),
        "actions": {
            "complete": "/payment-result",
            "cancel": "/payment-result"
        },
        "notes": {
            "booking_id": booking.id,
            "flight_instance_id": booking.flight_instance_id,
            "flight_number": booking.flight_number,
            "departure_time": booking.departure_time,
            "travel_class": booking.travel_class,
            "seats": booking.seats
        }
    }


def run_dummy_payment():
    if random.random() <= DUMMY_PAYMENT_SUCCESS_RATE:
        return {
            "success": True,
            "status": "paid",
            "failure_reason": None
        }

    return {
        "success": False,
        "status": "failed",
        "failure_reason": "Dummy gateway randomly failed the payment"
    }
