import math
import os
from collections import Counter
from datetime import datetime

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

try:
    import numpy as np
    from implicit.als import AlternatingLeastSquares
    from scipy.sparse import csr_matrix
except Exception:
    np = None
    AlternatingLeastSquares = None
    csr_matrix = None

import dbcon


BOOKING_WEIGHT = {
    "confirmed": 5.0,
    "pending": 2.0,
    "refunded": 0.2,
    "cancelled": 0.2,
    "expired": 0.1,
}
INTERACTION_WEIGHT = {
    "search_impression": 0.3,
    "booking_started": 2.0,
    "booking_confirmed": 5.0,
}
MODEL_DIR = os.getenv("RECOMMENDER_MODEL_DIR", "models")
MODEL_PATH = os.path.join(MODEL_DIR, "implicit_als_model.npz")
MIN_TRAINING_EVENTS = 2


def price_bucket(price):
    if price is None:
        return "price_unknown"
    if price < 5000:
        return "price_low"
    if price < 15000:
        return "price_mid"
    return "price_high"


def departure_bucket(departure_time):
    if not departure_time or departure_time == "estimated":
        return "departure_unknown"
    try:
        hour = int(str(departure_time).split("T", 1)[1][:2])
    except (IndexError, TypeError, ValueError):
        return "departure_unknown"

    if 5 <= hour < 12:
        return "departure_morning"
    if 12 <= hour < 17:
        return "departure_afternoon"
    if 17 <= hour < 22:
        return "departure_evening"
    return "departure_night"


def collect_training_data(db):
    bookings = db.query(dbcon.Booking).filter(
        dbcon.Booking.user_id.isnot(None),
        dbcon.Booking.flight_instance_id.isnot(None),
    ).all()
    interactions = db.query(dbcon.FlightInteraction).filter(
        dbcon.FlightInteraction.user_id.isnot(None),
        dbcon.FlightInteraction.flight_instance_id.isnot(None),
    ).all()

    flight_ids = {
        booking.flight_instance_id
        for booking in bookings
        if booking.flight_instance_id
    }
    flight_ids.update(
        interaction.flight_instance_id
        for interaction in interactions
        if interaction.flight_instance_id
    )

    users = db.query(dbcon.User).all()
    return users, bookings, interactions, sorted(flight_ids)


def build_training_interactions(bookings, interactions, user_mapping, item_mapping):
    training_interactions = []
    for booking in bookings:
        status = (booking.status or "").lower()
        if booking.user_id in user_mapping and booking.flight_instance_id in item_mapping:
            training_interactions.append((
                user_mapping[booking.user_id],
                item_mapping[booking.flight_instance_id],
                BOOKING_WEIGHT.get(status, 1.0),
            ))

    for interaction in interactions:
        event_type = (interaction.event_type or "").lower()
        if interaction.user_id in user_mapping and interaction.flight_instance_id in item_mapping:
            training_interactions.append((
                user_mapping[interaction.user_id],
                item_mapping[interaction.flight_instance_id],
                interaction.weight or INTERACTION_WEIGHT.get(event_type, 1.0),
            ))

    return training_interactions


def save_model_artifact(model, user_ids, item_ids, training_events):
    os.makedirs(MODEL_DIR, exist_ok=True)
    np.savez_compressed(
        MODEL_PATH,
        user_factors=model.user_factors,
        item_factors=model.item_factors,
        user_ids=np.array(user_ids, dtype=object),
        item_ids=np.array(item_ids, dtype=object),
        trained_at=np.array(datetime.utcnow().isoformat(), dtype=object),
        training_events=np.array(training_events, dtype=np.int64),
    )


def load_model_artifact():
    if np is None or not os.path.exists(MODEL_PATH):
        return None

    artifact = np.load(MODEL_PATH, allow_pickle=True)
    user_ids = [str(user_id) for user_id in artifact["user_ids"].tolist()]
    item_ids = [str(item_id) for item_id in artifact["item_ids"].tolist()]
    return {
        "user_factors": artifact["user_factors"],
        "item_factors": artifact["item_factors"],
        "user_mapping": {
            user_id: index
            for index, user_id in enumerate(user_ids)
        },
        "item_mapping": {
            item_id: index
            for index, item_id in enumerate(item_ids)
        },
        "trained_at": str(artifact["trained_at"].tolist()),
        "training_events": int(artifact["training_events"].tolist()),
    }


def train_implicit_model():
    if AlternatingLeastSquares is None or csr_matrix is None or np is None:
        return {
            "status": "skipped",
            "reason": "implicit is not installed",
        }

    db = dbcon.SESSION_LOCAL()
    try:
        users, bookings, interactions, item_ids = collect_training_data(db)
    finally:
        db.close()

    if not users or (not bookings and not interactions):
        return {
            "status": "skipped",
            "reason": "not enough interaction data",
        }

    user_ids = [user.id for user in users if user.id]
    if len(user_ids) < 1 or len(item_ids) < 2:
        return {
            "status": "skipped",
            "reason": "not enough users or flights",
            "users": len(user_ids),
            "items": len(item_ids),
        }

    user_mapping = {
        user_id: index
        for index, user_id in enumerate(user_ids)
    }
    item_mapping = {
        item_id: index
        for index, item_id in enumerate(item_ids)
    }

    training_interactions = build_training_interactions(
        bookings,
        interactions,
        user_mapping,
        item_mapping,
    )

    if len(training_interactions) < MIN_TRAINING_EVENTS:
        return {
            "status": "skipped",
            "reason": "not enough training events",
            "training_events": len(training_interactions),
        }

    rows = [row for row, _, _ in training_interactions]
    cols = [col for _, col, _ in training_interactions]
    values = [value for _, _, value in training_interactions]
    user_items = csr_matrix(
        (values, (rows, cols)),
        shape=(len(user_mapping), len(item_mapping)),
        dtype=np.float32,
    )

    if user_items.nnz < 2 or len(item_mapping) < 2:
        return {
            "status": "skipped",
            "reason": "interaction matrix is too small",
            "training_events": int(user_items.nnz),
        }

    model = AlternatingLeastSquares(
        factors=32,
        regularization=0.05,
        iterations=20,
        random_state=42,
    )
    model.fit(user_items)
    save_model_artifact(model, user_ids, item_ids, len(training_interactions))

    return {
        "status": "trained",
        "users": len(user_ids),
        "items": len(item_ids),
        "training_events": len(training_interactions),
        "model_path": MODEL_PATH,
    }


def implicit_scores(current_user, candidate_flights):
    if np is None:
        return None

    artifact = load_model_artifact()
    if not artifact:
        return None

    user_index = artifact["user_mapping"].get(current_user.id)
    if user_index is None:
        return None

    item_mapping = artifact["item_mapping"]
    scored_ids = [
        flight_id
        for flight_id in [
            flight.get("flight_instance_id")
            for flight in candidate_flights
            if flight.get("flight_instance_id")
        ]
        if flight_id in item_mapping
    ]
    if not scored_ids:
        return None

    user_vector = artifact["user_factors"][user_index]
    predictions = [
        float(np.dot(user_vector, artifact["item_factors"][item_mapping[flight_id]]))
        for flight_id in scored_ids
    ]

    return {
        flight_id: float(score)
        for flight_id, score in zip(scored_ids, predictions)
    }


def user_preference_profile(db, user_id):
    bookings = db.query(dbcon.Booking).filter(
        dbcon.Booking.user_id == user_id
    ).all()
    if not bookings:
        return {}

    classes = Counter()
    flight_numbers = Counter()
    total_seats = 0
    total_amount = 0

    for booking in bookings:
        classes[booking.travel_class or "economy"] += 1
        flight_numbers[booking.flight_number or ""] += 1
        total_seats += booking.seats or 1
        total_amount += booking.amount or 0

    return {
        "preferred_class": classes.most_common(1)[0][0],
        "flight_numbers": flight_numbers,
        "average_ticket_value": total_amount / max(total_seats, 1),
    }


def fallback_score(flight, profile):
    economy_price = (flight.get("ticket_price") or {}).get("economy") or 0
    seats = flight.get("seat_availability") or {}

    score = 0.0
    if economy_price:
        score += 1 / math.log(economy_price + 10)
    score += min((seats.get("economy") or 0) / 160, 1) * 0.25
    score += min((seats.get("business") or 0) / 30, 1) * 0.15

    preferred_class = profile.get("preferred_class")
    if preferred_class in {"economy", "business"}:
        score += 0.2

    if flight.get("flight_number") in (profile.get("flight_numbers") or {}):
        score += 0.25

    average_ticket_value = profile.get("average_ticket_value")
    if average_ticket_value and economy_price:
        gap = abs(economy_price - average_ticket_value)
        score += max(0, 0.25 - (gap / max(average_ticket_value, 1)))

    return score


def recommendation_reason(flight, profile, source):
    reasons = []
    if source == "implicit":
        reasons.append("Implicit ALS personalized ranking")
    if profile.get("preferred_class"):
        reasons.append(f"matches your {profile['preferred_class']} preference")
    if (flight.get("ticket_price") or {}).get("economy"):
        reasons.append("strong price fit")
    if (flight.get("seat_availability") or {}).get("economy", 0) > 0:
        reasons.append("available seats")
    return ", ".join(reasons[:3]) or "best overall match"


def rank_flights_for_user(current_user, flights):
    if not flights:
        return flights

    db = dbcon.SESSION_LOCAL()
    try:
        profile = user_preference_profile(db, current_user.id)
        ml_scores = implicit_scores(current_user, flights)
        source = "implicit" if ml_scores else "fallback"

        ranked = []
        for flight in flights:
            flight_id = flight.get("flight_instance_id")
            score = (
                ml_scores.get(flight_id)
                if ml_scores and flight_id in ml_scores
                else fallback_score(flight, profile)
            )
            enriched = dict(flight)
            enriched["recommendation"] = {
                "score": round(float(score), 6),
                "source": source,
                "reason": recommendation_reason(flight, profile, source),
                "is_recommended": False,
            }
            ranked.append(enriched)

        ranked.sort(
            key=lambda flight: flight["recommendation"]["score"],
            reverse=True
        )
        ranked[0]["recommendation"]["is_recommended"] = True
        return ranked
    finally:
        db.close()
