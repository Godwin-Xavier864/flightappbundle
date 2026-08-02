import os
import json
import time

import redis


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
RESERVATION_TTL_SECONDS = int(os.getenv("RESERVATION_TTL_SECONDS", "900"))
FLIGHT_SEARCH_CACHE_TTL_SECONDS = int(os.getenv("FLIGHT_SEARCH_CACHE_TTL_SECONDS", "300"))

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)


RESERVE_SCRIPT = """
local seat_key = KEYS[1]
local holds_key = KEYS[2]
local hold_key = KEYS[3]
local base_field = ARGV[1]
local seats = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local expires_at = now + ttl

local expired_hold_keys = redis.call("ZRANGEBYSCORE", holds_key, "-inf", now)
for _, expired_hold_key in ipairs(expired_hold_keys) do
    local expired_seats = redis.call("HGET", expired_hold_key, "seats")
    if expired_seats then
        redis.call("HINCRBY", seat_key, base_field, tonumber(expired_seats))
        redis.call("DEL", expired_hold_key)
    end
    redis.call("ZREM", holds_key, expired_hold_key)
end

local visible_available = tonumber(redis.call("HGET", seat_key, base_field) or "0")

if redis.call("EXISTS", hold_key) == 1 then
    return {1, visible_available}
end

if visible_available < seats then
    return {0, visible_available}
end

local remaining = redis.call("HINCRBY", seat_key, base_field, -seats)

redis.call(
    "HSET",
    hold_key,
    "seats", seats,
    "expires_at", expires_at
)
redis.call("EXPIRE", hold_key, ttl)
redis.call("ZADD", holds_key, expires_at, hold_key)

return {1, remaining}
"""


reserve_script = redis_client.register_script(RESERVE_SCRIPT)


def seat_key(flight_instance_id):
    return f"flight_instance:{flight_instance_id}:seats"


def holds_key(flight_instance_id, travel_class):
    return f"flight_instance:{flight_instance_id}:holds:{travel_class}"


def hold_key(idempotency_key):
    return f"booking_hold:{idempotency_key}"


def flight_search_cache_key(from_city, to_city):
    return f"flight_search:{from_city.strip().lower()}:{to_city.strip().lower()}"


def flight_search_lock_key(from_city, to_city):
    return f"{flight_search_cache_key(from_city, to_city)}:lock"


def flight_channel(flight_instance_id):
    return f"flight_instance:{flight_instance_id}:seat_updates"


def get_cached_flight_search(from_city, to_city):
    cached = redis_client.get(flight_search_cache_key(from_city, to_city))
    if not cached:
        return None
    return json.loads(cached)


def set_cached_flight_search(from_city, to_city, payload):
    redis_client.setex(
        flight_search_cache_key(from_city, to_city),
        FLIGHT_SEARCH_CACHE_TTL_SECONDS,
        json.dumps(payload)
    )


def acquire_flight_search_lock(from_city, to_city, ttl_seconds=20):
    return redis_client.set(
        flight_search_lock_key(from_city, to_city),
        "1",
        nx=True,
        ex=ttl_seconds
    )


def release_flight_search_lock(from_city, to_city):
    redis_client.delete(flight_search_lock_key(from_city, to_city))


def publish_seat_update(flight_instance_id, availability):
    redis_client.publish(
        flight_channel(flight_instance_id),
        json.dumps({
            "flight_instance_id": flight_instance_id,
            "seat_availability": availability,
        })
    )


def ensure_flight_cache(seat_record):
    key = seat_key(seat_record.flight_instance_id)
    if redis_client.exists(key):
        return

    sync_flight_cache(seat_record)


def active_reserved(flight_instance_id, travel_class):
    key = holds_key(flight_instance_id, travel_class)
    now = int(time.time())
    expired_hold_keys = redis_client.zrangebyscore(key, "-inf", now)

    for expired_hold_key in expired_hold_keys:
        seats = redis_client.hget(expired_hold_key, "seats")
        if seats is not None:
            redis_client.hincrby(
                seat_key(flight_instance_id),
                f"{travel_class}_available",
                int(seats)
            )
            redis_client.delete(expired_hold_key)
        redis_client.zrem(key, expired_hold_key)

    total = 0

    for active_hold_key in redis_client.zrange(key, 0, -1):
        seats = redis_client.hget(active_hold_key, "seats")
        if seats is None:
            redis_client.zrem(key, active_hold_key)
            continue
        total += int(seats)

    return total


def cached_availability(seat_record):
    ensure_flight_cache(seat_record)
    economy_reserved = active_reserved(seat_record.flight_instance_id, "economy")
    business_reserved = active_reserved(seat_record.flight_instance_id, "business")
    key = seat_key(seat_record.flight_instance_id)
    economy_available = int(redis_client.hget(key, "economy_available") or 0)
    business_available = int(redis_client.hget(key, "business_available") or 0)

    return {
        "economy": max(economy_available, 0),
        "business": max(business_available, 0),
        "reserved": {
            "economy": economy_reserved,
            "business": business_reserved,
        },
        "departure_time": seat_record.departure_time,
    }


def reserve_seats(seat_record, travel_class, seats, idempotency_key):
    ensure_flight_cache(seat_record)
    now = int(time.time())
    result = reserve_script(
        keys=[
            seat_key(seat_record.flight_instance_id),
            holds_key(seat_record.flight_instance_id, travel_class),
            hold_key(idempotency_key),
        ],
        args=[
            f"{travel_class}_available",
            seats,
            RESERVATION_TTL_SECONDS,
            now,
        ],
    )

    reserved = int(result[0]) == 1
    remaining = max(int(result[1]), 0)

    if reserved:
        redis_client.hset(
            hold_key(idempotency_key),
            mapping={
                "flight_number": seat_record.flight_number,
                "flight_instance_id": seat_record.flight_instance_id,
                "departure_time": seat_record.departure_time or "",
                "travel_class": travel_class,
                "seats": seats,
                "expires_at": now + RESERVATION_TTL_SECONDS,
            }
        )
        redis_client.expire(hold_key(idempotency_key), RESERVATION_TTL_SECONDS)

    return reserved, remaining, now + RESERVATION_TTL_SECONDS


def restore_hold(seat_record, booking, ttl_seconds):
    if ttl_seconds <= 0:
        return False

    ensure_flight_cache(seat_record)
    expires_at = int(time.time()) + int(ttl_seconds)
    key = hold_key(booking.idempotency_key)

    redis_client.hset(
        key,
        mapping={
            "flight_number": seat_record.flight_number,
            "flight_instance_id": seat_record.flight_instance_id,
            "departure_time": seat_record.departure_time or "",
            "travel_class": booking.travel_class,
            "seats": booking.seats,
            "expires_at": expires_at,
            "restored_from_db": "1",
        }
    )
    redis_client.expire(key, int(ttl_seconds))
    redis_client.hincrby(
        seat_key(seat_record.flight_instance_id),
        f"{booking.travel_class}_available",
        -int(booking.seats)
    )
    redis_client.zadd(
        holds_key(seat_record.flight_instance_id, booking.travel_class),
        {key: expires_at}
    )
    return True


def get_hold(idempotency_key):
    hold = redis_client.hgetall(hold_key(idempotency_key))
    return hold or None


def release_hold(idempotency_key, return_seats=True):
    hold = get_hold(idempotency_key)
    if not hold:
        return

    if return_seats:
        redis_client.hincrby(
            seat_key(hold["flight_instance_id"]),
            f"{hold['travel_class']}_available",
            int(hold["seats"])
        )

    redis_client.zrem(
        holds_key(hold["flight_instance_id"], hold["travel_class"]),
        hold_key(idempotency_key)
    )
    redis_client.delete(hold_key(idempotency_key))


def sync_flight_cache(seat_record):
    economy_reserved = active_reserved(seat_record.flight_instance_id, "economy")
    business_reserved = active_reserved(seat_record.flight_instance_id, "business")
    redis_client.hset(
        seat_key(seat_record.flight_instance_id),
        mapping={
            "flight_instance_id": seat_record.flight_instance_id,
            "flight_number": seat_record.flight_number,
            "departure_time": seat_record.departure_time or "",
            "economy_available": max((seat_record.economy_available or 0) - economy_reserved, 0),
            "business_available": max((seat_record.business_available or 0) - business_reserved, 0),
            "economy_price": seat_record.economy_price or 0,
            "business_price": seat_record.business_price or 0,
        }
    )
    return cached_availability(seat_record)
