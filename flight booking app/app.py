import uvicorn
import requests
import os
import asyncio
import json
import hashlib
from datetime import datetime
from settings import setup_middleware
from payment_service import (
    create_dummy_payment_session,
    new_payment_order_id,
    run_dummy_payment,
)
from itinerary_service import generate_itinerary
from recommender_service import rank_flights_for_user

from fastapi import FastAPI
import dbcon
import redis_seats
import reservation_recovery
from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordBearer

from jose import jwt, JWTError
from passlib.context import CryptContext
from redis.exceptions import RedisError
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from schema import (
    Signup,
    Login,
    BookTicket,
    CreateItinerary,
    PaymentResult,
    RefundRequest,
    AdminRefundDecision,
    AgentChatRequest,
)
import random, math
from agent_service import run_flight_agent
app= FastAPI()
setup_middleware(app)


@app.on_event("startup")
def recover_reservations_on_startup():
    try:
        reservation_recovery.recover_reservation_state()
    except RedisError:
        pass


def load_env_file(path=".env"):
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()


pwd_context = CryptContext(schemes=["bcrypt"],)

def hash_password(password):
    return pwd_context.hash(password)
 
def verify_password(password, hashed_password):
    return pwd_context.verify(password, hashed_password)

SECRET_KEY = os.getenv("SECRET_KEY", "testingmysecretkey123")
ALGORITHM = "HS256"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

def create_access_token(username):
    return jwt.encode(
        {"sub": username},
        SECRET_KEY,
        algorithm=ALGORITHM
    )
    
def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Invalid Token"
    )

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )

        username = payload.get("sub")

        if username is None:
            raise credentials_exception

    except JWTError:
        raise credentials_exception

    db = dbcon.SESSION_LOCAL()

    user = db.query(dbcon.User).filter(
        dbcon.User.username == username
    ).first()

    db.close()

    if user is None:
        raise credentials_exception

    return user


def get_current_admin(current_user: dbcon.User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def resolve_ticket_route(db, booking):
    if getattr(booking, "from_city", None) and getattr(booking, "to_city", None):
        return booking.from_city, booking.to_city

    flight_num = booking.flight_number or ""
    flight_inst = booking.flight_instance_id or ""

    dep_city, arr_city = None, None
    if db:
        if len(flight_num) >= 8 and flight_num[:2].isalpha():
            dep_code = flight_num[2:5].upper()
            arr_code = flight_num[5:8].upper()
            dep_apt = db.query(dbcon.Airport).filter(dbcon.Airport.iata_code == dep_code).first()
            arr_apt = db.query(dbcon.Airport).filter(dbcon.Airport.iata_code == arr_code).first()
            if dep_apt: dep_city = dep_apt.city or dep_apt.airport_name
            if arr_apt: arr_city = arr_apt.city or arr_apt.airport_name

        if not dep_city or not arr_city:
            parts = flight_inst.split("-")
            if len(parts) >= 2:
                dep_code = parts[0].upper()
                arr_code = parts[1].upper()
                dep_apt = db.query(dbcon.Airport).filter(dbcon.Airport.iata_code == dep_code).first()
                arr_apt = db.query(dbcon.Airport).filter(dbcon.Airport.iata_code == arr_code).first()
                if dep_apt: dep_city = dep_apt.city or dep_apt.airport_name or parts[0].title()
                if arr_apt: arr_city = arr_apt.city or arr_apt.airport_name or parts[1].title()

    return dep_city or "Origin", arr_city or "Destination"


def serialize_ticket(booking, db=None):
    from_city = getattr(booking, "from_city", None)
    to_city = getattr(booking, "to_city", None)

    if not from_city or not to_city:
        from_c, to_c = resolve_ticket_route(db, booking)
        from_city = from_city or from_c
        to_city = to_city or to_c

    return {
        "booking_id": booking.id,
        "flight_instance_id": booking.flight_instance_id,
        "flight_number": booking.flight_number,
        "departure_time": booking.departure_time,
        "travel_class": booking.travel_class,
        "seats": booking.seats,
        "amount": booking.amount,
        "status": booking.status,
        "from": from_city or "Origin",
        "to": to_city or "Destination",
        "payment_order_id": booking.payment_order_id,
        "idempotency_key": booking.idempotency_key,
        "reservation_expires_at": (
            booking.reservation_expires_at.isoformat()
            if booking.reservation_expires_at
            else None
        ),
        "refund": {
            "status": booking.refund_status or "none",
            "reason": booking.refund_reason,
            "requested_at": (
                booking.refund_requested_at.isoformat()
                if booking.refund_requested_at
                else None
            ),
            "admin_note": booking.refund_admin_note,
            "resolved_at": (
                booking.refund_resolved_at.isoformat()
                if booking.refund_resolved_at
                else None
            ),
        }
    }



@app.post("/signup")
async def signup(user: Signup):

    db = dbcon.SESSION_LOCAL()

    existing = db.query(dbcon.User).filter(
        dbcon.User.username == user.username
    ).first()

    if existing:
        db.close()
        return {"message": "Username already exists"}

    new_user = dbcon.User(
        username=user.username,
        email=user.email,
        hashed_password=hash_password(user.password)
    )

    db.add(new_user)
    db.commit()
    db.close()

    return {"message": "Signup Successful"}



@app.post("/login")
async def login(user: Login):

    db = dbcon.SESSION_LOCAL()

    db_user = db.query(dbcon.User).filter(
        dbcon.User.username == user.username
    ).first()

    db.close()

    if db_user is None:
        raise HTTPException(status_code=401, detail="Invalid username")

    if not verify_password(user.password, db_user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid password")

    token = create_access_token(db_user.username)

    return {
        "access_token": token,
        "token_type": "bearer"
    }





AVIATIONSTACK_API_KEY = os.getenv("AVIATIONSTACK_API_KEY", "")
REQUEST_TIMEOUT_SECONDS = 10
OVERPASS_API_URL = os.getenv("OVERPASS_URL","")
OSM_SEARCH_RADIUS_METERS = 5000
CLASS_MULTIPLIERS = {
    "economy": 1,
    "business": 2.4
}
PREFERRED_CITY_AIRPORTS = {
    "london": ["LHR", "LGW", "LCY", "STN", "LTN"],
    "delhi": ["DEL"],
    "new delhi": ["DEL"],
    "mumbai": ["BOM"],
    "bangalore": ["BLR"],
    "bengaluru": ["BLR"],
    "chennai": ["MAA"],
    "kolkata": ["CCU"],
    "paris": ["CDG", "ORY"],
    "new york": ["JFK", "LGA", "EWR"],
    "dubai": ["DXB"],
    "singapore": ["SIN"],
}

#Price simulaition using distance
def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    lat1 = math.radians(lat1)
    lon1 = math.radians(lon1)
    lat2 = math.radians(lat2)
    lon2 = math.radians(lon2)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(dlon / 2) ** 2
    )

    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def simulate_price(distance_km):
    base = 2000
    per_km = 4.5
    demand = random.uniform(0.95, 1.10)
    return round((base + distance_km * per_km) * demand)


def normalize_travel_class(travel_class):
    normalized = travel_class.strip().lower()

    if normalized not in CLASS_MULTIPLIERS:
        raise HTTPException(
            status_code=400,
            detail="travel_class must be economy or business"
        )

    return normalized


def class_prices(economy_price):
    if not economy_price:
        return {
            "economy": None,
            "business": None
        }

    return {
        "economy": economy_price,
        "business": round(economy_price * CLASS_MULTIPLIERS["business"])
    }


def build_flight_instance_id(flight_number, departure_time):
    raw = f"{flight_number}|{departure_time or 'unknown'}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{flight_number}:{digest}"


def get_or_create_flight_seats(db, flight_number, departure_time):
    flight_instance_id = build_flight_instance_id(flight_number, departure_time)
    seat_record = db.query(dbcon.FlightSeat).filter(
        dbcon.FlightSeat.flight_instance_id == flight_instance_id
    ).first()

    if seat_record:
        return seat_record

    seat_record = dbcon.FlightSeat(
        flight_instance_id=flight_instance_id,
        flight_number=flight_number,
        departure_time=departure_time,
        economy_available=random.randint(80, 160),
        business_available=random.randint(8, 30)
    )
    db.add(seat_record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        seat_record = db.query(dbcon.FlightSeat).filter(
            dbcon.FlightSeat.flight_instance_id == flight_instance_id
        ).first()
        if seat_record:
            return seat_record
        raise

    db.refresh(seat_record)

    return seat_record


def add_booking_options(flight, economy_price):
    db = dbcon.SESSION_LOCAL()

    try:
        seat_record = get_or_create_flight_seats(
            db,
            flight["flight_number"],
            flight.get("departure_time")
        )
        flight["flight_instance_id"] = seat_record.flight_instance_id
        prices = class_prices(economy_price)

        if prices["economy"] and prices["business"]:
            seat_record.economy_price = prices["economy"]
            seat_record.business_price = prices["business"]
            db.commit()

        flight["ticket_price"] = {
                    "economy": prices["economy"],
                    "business": prices["business"]
                }
        flight["price_difference"] = {
                    "business_extra": prices["business"] - prices["economy"]
                }
        try:
            flight["seat_availability"] = redis_seats.cached_availability(seat_record)
        except RedisError:
            flight["seat_availability"] = {
                "economy": seat_record.economy_available,
                "business": seat_record.business_available,
                "reserved": {
                    "economy": 0,
                    "business": 0
                }
            }

        return flight
    finally:
        db.close()


def booking_response(existing_booking, idempotency_key):
    payment_session = create_dummy_payment_session(
        existing_booking.amount,
        existing_booking,
        idempotency_key
    )

    return {
        "message": "Ticket booking already exists for this idempotency key",
        "booking_id": existing_booking.id,
        "flight_instance_id": existing_booking.flight_instance_id,
        "flight_number": existing_booking.flight_number,
        "departure_time": existing_booking.departure_time,
        "travel_class": existing_booking.travel_class,
        "seats_booked": existing_booking.seats,
        "status": existing_booking.status,
        "amount": existing_booking.amount,
        "payment_session": payment_session
    }


def log_flight_interaction(db, current_user, flight, event_type, weight):
    interaction = dbcon.FlightInteraction(
        user_id=current_user.id,
        flight_instance_id=flight.get("flight_instance_id"),
        flight_number=flight.get("flight_number"),
        event_type=event_type,
        route_from=flight.get("departure_iata"),
        route_to=flight.get("arrival_iata"),
        weight=weight,
        created_at=datetime.utcnow()
    )
    db.add(interaction)


def log_search_impressions(current_user, flights):
    db = dbcon.SESSION_LOCAL()
    try:
        for flight in flights:
            if flight.get("flight_instance_id"):
                log_flight_interaction(
                    db,
                    current_user,
                    flight,
                    "search_impression",
                    0.3
                )
        db.commit()
    finally:
        db.close()


def fetch_json(url):
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS) #since api call may take time, added a timeout
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None
    except ValueError:
        return None


def sse_event(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def flight_cache_payload(payload):
    cached_payload = dict(payload)
    cached_payload["flights"] = []

    for flight in payload.get("flights", []):
        cached_flight = dict(flight)
        cached_flight.pop("seat_availability", None)
        cached_payload["flights"].append(cached_flight)

    cached_payload["cache"] = "hit"
    return cached_payload


async def wait_for_flight_search_cache(from_city, to_city, attempts=10):
    for _ in range(attempts):
        await asyncio.sleep(0.5)
        cached_response = redis_seats.get_cached_flight_search(from_city, to_city)
        if cached_response:
            return cached_response
    return None


def refresh_cached_seat_availability(cached_response):
    db = dbcon.SESSION_LOCAL()
    try:
        for flight in cached_response.get("flights", []):
            seat_record = db.query(dbcon.FlightSeat).filter(
                dbcon.FlightSeat.flight_instance_id == flight["flight_instance_id"]
            ).first()
            if seat_record:
                flight["seat_availability"] = redis_seats.cached_availability(seat_record)
        cached_response["cache"] = "hit"
        return cached_response
    finally:
        db.close()


def fetch_overpass_json(query):
    try:
        response = requests.post(
            OVERPASS_API_URL,
            data={"data": query},
            timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None
    except ValueError:
        return None



#Extracts the latitude and longitude of each place returned by OpenstreetMap.
def osm_element_location(element):
    if "lat" in element and "lon" in element:
        return element["lat"], element["lon"]

    center = element.get("center")
    if center:
        return center.get("lat"), center.get("lon")

    return None, None

#Converts the raw OpenStreetMap response into a simpl format
def format_osm_places(elements, category, limit=8):
    places = []

    for element in elements:
        tags = element.get("tags", {})
        name = tags.get("name")

        if not name:
            continue

        latitude, longitude = osm_element_location(element)

        places.append({
            "name": name,
            "category": category,
            "type": (
                tags.get("tourism")
                or tags.get("historic")
                or tags.get("leisure")
                or tags.get("amenity")
            ),
            "address": tags.get("addr:full") or tags.get("addr:street"),
            "latitude": latitude,
            "longitude": longitude,
            "source": "openstreetmap"
        })

        if len(places) == limit:
            break

    return places

#takes the destination latitude and longitude,
# Sends a query to the Overpass API searches for nearby tourist attractions



def fetch_destination_places(latitude, longitude):
    radius = OSM_SEARCH_RADIUS_METERS
    query = f"""
    [out:json][timeout:10];
    (
      nwr(around:{radius},{latitude},{longitude})["tourism"~"attraction|museum|gallery|viewpoint|zoo|theme_park|aquarium"];
      nwr(around:{radius},{latitude},{longitude})["historic"];
      nwr(around:{radius},{latitude},{longitude})["leisure"="park"];
      nwr(around:{radius},{latitude},{longitude})["tourism"~"hotel|guest_house|hostel|motel|apartment"];
      nwr(around:{radius},{latitude},{longitude})["amenity"~"restaurant|cafe|fast_food"];
    );
    out center tags 50;
    """

    data = fetch_overpass_json(query)

    if not data or "elements" not in data:
        return {
            "tourist_spots": [],
            "hotels": [],
            "restaurants": [],
            "source": "openstreetmap",
            "status": "unavailable"
        }

    tourist_elements = []
    hotel_elements = []
    restaurant_elements = []

    for element in data["elements"]:
        tags = element.get("tags", {})

        if tags.get("tourism") in {"hotel", "guest_house", "hostel", "motel", "apartment"}:
            hotel_elements.append(element)
        elif tags.get("amenity") in {"restaurant", "cafe", "fast_food"}:
            restaurant_elements.append(element)
        else:
            tourist_elements.append(element)

    return {
        "tourist_spots": format_osm_places(tourist_elements, "tourist_spot"),
        "hotels": format_osm_places(hotel_elements, "hotel"),
        "restaurants": format_osm_places(restaurant_elements, "restaurant"),
        "source": "openstreetmap",
        "status": "ok"
    }


def format_weather_forecast(weather_data):
    daily = (weather_data or {}).get("daily", {})
    dates = daily.get("time", [])
    max_temperatures = daily.get("temperature_2m_max", [])
    min_temperatures = daily.get("temperature_2m_min", [])
    rain_totals = daily.get("precipitation_sum", [])
    wind_speeds = daily.get("wind_speed_10m_max", [])

    forecast = []

    for index, date in enumerate(dates[:3]):
        forecast.append({
            "date": date,
            "temperature_max": max_temperatures[index] if index < len(max_temperatures) else None,
            "temperature_min": min_temperatures[index] if index < len(min_temperatures) else None,
            "precipitation_sum": rain_totals[index] if index < len(rain_totals) else None,
            "wind_speed_max": wind_speeds[index] if index < len(wind_speeds) else None
        })

    return forecast


def find_local_airport(location):
    db = dbcon.SESSION_LOCAL()
    try:
        normalized_location = location.strip().lower()
        airport_code = normalized_location.upper()
        preferred_codes = PREFERRED_CITY_AIRPORTS.get(normalized_location, [])

        if preferred_codes:
            preferred_airports = db.query(dbcon.Airport).filter(
                dbcon.Airport.iata_code.in_(preferred_codes)
            ).all()
            by_code = {
                airport.iata_code: airport
                for airport in preferred_airports
            }

            for code in preferred_codes:
                if code in by_code:
                    return by_code[code]

        exact_airport = db.query(dbcon.Airport).filter(
            or_(
                dbcon.Airport.iata_code == airport_code,
                dbcon.Airport.icao_code == airport_code,
                dbcon.Airport.airport_name.ilike(location)
            )
        ).first()

        if exact_airport:
            return exact_airport

        return db.query(dbcon.Airport).filter(
            or_(
                dbcon.Airport.airport_name.ilike(f"%{location}%"),
                dbcon.Airport.city.ilike(f"%{location}%")
            )
        ).first()
    finally:
        db.close()


def airport_info_from_local(local_airport):
    return {
        "name": local_airport.airport_name,
        "iata": local_airport.iata_code,
        "icao": local_airport.icao_code,
        "city": local_airport.city,
        "country": local_airport.country,
        "latitude": local_airport.latitude,
        "longitude": local_airport.longitude,
        "source": "local"
    }


def find_airport_for_location(location):
    airport_data = None
    local_airport = find_local_airport(location)

    if local_airport:
        return airport_info_from_local(local_airport)

    if AVIATIONSTACK_API_KEY:
        airport_url = (
            "https://api.aviationstack.com/v1/airports"
            f"?access_key={AVIATIONSTACK_API_KEY}"
            f"&search={location}"
        )
        airport_data = fetch_json(airport_url)

    if airport_data and "data" in airport_data and len(airport_data["data"]) > 0:
        airport = airport_data["data"][0]
        local_match = find_local_airport(
            airport.get("iata_code")
            or airport.get("icao_code")
            or airport.get("airport_name")
            or location
        )

        if local_match:
            return airport_info_from_local(local_match)

        return {
            "name": airport.get("airport_name"),
            "iata": airport.get("iata_code"),
            "icao": airport.get("icao_code"),
            "city": airport.get("city_name") or location,
            "country": airport.get("country_name"),
            "latitude": None,
            "longitude": None,
            "source": "aviationstack"
        }

    return {
        "name": None,
        "iata": None,
        "icao": None,
        "city": location,
        "country": None,
        "latitude": None,
        "longitude": None,
        "source": "unavailable"
    }


def find_airport_for_city(city):
    return find_airport_for_location(city)


def build_route_fallback_flight(from_city, to_city, departure_airport, arrival_airport):
    if not departure_airport or not arrival_airport:
        return []

    distance = calculate_distance(
        departure_airport.latitude,
        departure_airport.longitude,
        arrival_airport.latitude,
        arrival_airport.longitude
    )
    route_code = f"{departure_airport.iata_code}{arrival_airport.iata_code}"
    airlines = [
        "Fallback Airways",
        "SkyConnect",
        "AeroLink",
        "JetRoute",
        "CloudLine"
    ]
    departures = [
        ("06:15", "08:05"),
        ("09:40", "11:30"),
        ("13:20", "15:10"),
        ("17:45", "19:35"),
        ("21:10", "23:00")
    ]
    price_multipliers = [0.96, 1.02, 1.08, 0.99, 1.12]
    base_price = simulate_price(distance)
    flights = []

    for index, (departure_time, arrival_time) in enumerate(departures, start=1):
        ticket_price = round(base_price * price_multipliers[index - 1])
        flight = {
            "flight_number": f"FB{route_code}{index}",
            "airline": airlines[index - 1],
            "from": departure_airport.airport_name,
            "to": arrival_airport.airport_name,
            "from_city": from_city.title(),
            "to_city": to_city.title(),
            "departure_iata": departure_airport.iata_code,
            "arrival_iata": arrival_airport.iata_code,
            "distance_km": round(distance, 2),
            "departure_time": departure_time,
            "arrival_time": arrival_time,
            "status": "estimated",
            "source": "local_route_fallback"
        }
        flights.append(add_booking_options(flight, ticket_price))

    return flights


def build_fallback_flights(departure_airport):
    db = dbcon.SESSION_LOCAL()
    try:
        arrival_airports = db.query(dbcon.Airport).filter(
            dbcon.Airport.iata_code != departure_airport.iata_code,
            dbcon.Airport.latitude.isnot(None),
            dbcon.Airport.longitude.isnot(None)
        ).limit(5).all()

        flights = []

        for index, arrival_airport in enumerate(arrival_airports, start=1):
            distance = calculate_distance(
                departure_airport.latitude,
                departure_airport.longitude,
                arrival_airport.latitude,
                arrival_airport.longitude
            )
            ticket_price = simulate_price(distance)

            flight = {
                "flight_number": f"FB{100 + index}",
                "airline": "Fallback Airways",
                "from": departure_airport.airport_name,
                "to": arrival_airport.airport_name,
                "departure_iata": departure_airport.iata_code,
                "arrival_iata": arrival_airport.iata_code,
                "distance_km": round(distance, 2),
                "departure_time": "estimated",
                "arrival_time": None,
                "status": "estimated",
                "source": "local_fallback"
            }

            flights.append(add_booking_options(flight, ticket_price))

        return flights
    finally:
        db.close()


@app.post("/book-ticket")
async def book_ticket(
    ticket: BookTicket,
    current_user: dbcon.User = Depends(get_current_user)
):
    travel_class = normalize_travel_class(ticket.travel_class)

    if ticket.seats < 1:
        raise HTTPException(
            status_code=400,
            detail="seats must be at least 1"
        )

    db = dbcon.SESSION_LOCAL()

    try:
        existing_booking = db.query(dbcon.Booking).filter(
            dbcon.Booking.idempotency_key == ticket.idempotency_key
        ).first()

        if existing_booking:
            return booking_response(existing_booking, ticket.idempotency_key)

        seat_record = db.query(dbcon.FlightSeat).filter(
            dbcon.FlightSeat.flight_instance_id == ticket.flight_instance_id
        ).first()

        if not seat_record:
            raise HTTPException(
                status_code=404,
                detail="Flight not found. Search flights first before booking."
            )

        available_field = f"{travel_class}_available"
        price_field = f"{travel_class}_price"
        price = getattr(seat_record, price_field)

        if not price:
            raise HTTPException(
                status_code=400,
                detail="Price is unavailable for this flight. Please refresh flight search."
            )

        try:
            reserved, remaining_seats, expires_at = redis_seats.reserve_seats(
                seat_record,
                travel_class,
                ticket.seats,
                ticket.idempotency_key
            )
        except RedisError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Seat reservation cache is unavailable: {exc}"
            )

        if not reserved:
            raise HTTPException(
                status_code=400,
                detail=f"Only {remaining_seats} {travel_class} seats available"
            )

        amount = price * ticket.seats
        order_id = new_payment_order_id()
        reservation_expires_at = datetime.utcfromtimestamp(expires_at)

        booking = dbcon.Booking(
            flight_instance_id=seat_record.flight_instance_id,
            flight_number=seat_record.flight_number,
            departure_time=seat_record.departure_time or ticket.departure_time,
            travel_class=travel_class,
            seats=ticket.seats,
            amount=amount,
            payment_order_id=order_id,
            idempotency_key=ticket.idempotency_key,
            status="pending",
            reservation_expires_at=reservation_expires_at,
            user_id=current_user.id
        )

        db.add(booking)
        log_flight_interaction(
            db,
            current_user,
            {
                "flight_instance_id": seat_record.flight_instance_id,
                "flight_number": seat_record.flight_number,
            },
            "booking_started",
            2.0
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            redis_seats.release_hold(ticket.idempotency_key)
            existing_booking = db.query(dbcon.Booking).filter(
                dbcon.Booking.idempotency_key == ticket.idempotency_key
            ).first()

            if existing_booking:
                return booking_response(existing_booking, ticket.idempotency_key)

            raise

        db.refresh(booking)
        db.refresh(seat_record)

        availability = redis_seats.sync_flight_cache(seat_record)
        redis_seats.publish_seat_update(ticket.flight_instance_id, availability)

        payment_session = create_dummy_payment_session(amount, booking, ticket.idempotency_key)

        return {
            "message": "Seats reserved. Complete payment before the reservation expires.",
            "booking_id": booking.id,
            "flight_instance_id": booking.flight_instance_id,
            "flight_number": booking.flight_number,
            "departure_time": booking.departure_time,
            "travel_class": travel_class,
            "seats_reserved": ticket.seats,
            "status": booking.status,
            "reservation_expires_at": reservation_expires_at.isoformat(),
            "remaining_seats": {
                travel_class: remaining_seats
            },
            "amount": amount,
            "payment_session": payment_session
        }
    finally:
        db.close()


@app.post("/payment-result")
async def payment_result(
    payment: PaymentResult,
    current_user: dbcon.User = Depends(get_current_user)
):
    db = dbcon.SESSION_LOCAL()

    try:
        booking = db.query(dbcon.Booking).filter(
            dbcon.Booking.idempotency_key == payment.idempotency_key,
            dbcon.Booking.user_id == current_user.id
        ).first()

        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")

        if booking.status == "confirmed":
            return {
                "message": "Payment already confirmed",
                "booking_id": booking.id,
                "status": booking.status
            }

        if booking.status != "pending":
            return {
                "message": f"Booking is already {booking.status}",
                "booking_id": booking.id,
                "status": booking.status
            }

        action = payment.action.strip().lower()

        if action not in {"complete", "cancel"}:
            raise HTTPException(
                status_code=400,
                detail="payment action must be complete or cancel"
            )

        if action == "cancel" or payment.success is False:
            transitioned = reservation_recovery.transition_pending_status(
                db,
                booking,
                "payment_failed"
            )
            db.commit()
            if transitioned:
                redis_seats.release_hold(payment.idempotency_key, return_seats=True)
            seat_record = db.query(dbcon.FlightSeat).filter(
                dbcon.FlightSeat.flight_instance_id == booking.flight_instance_id
            ).first()
            if seat_record:
                availability = redis_seats.sync_flight_cache(seat_record)
                redis_seats.publish_seat_update(booking.flight_instance_id, availability)
            return {
                "message": "Payment cancelled. Reserved seats were released.",
                "booking_id": booking.id,
                "status": booking.status,
                "payment": {
                    "provider": "dummy",
                    "status": "cancelled",
                    "failure_reason": "User cancelled the payment"
                }
            }

        hold = redis_seats.get_hold(payment.idempotency_key)
        if not hold:
            restored = reservation_recovery.recover_booking_hold(db, booking)
            hold = redis_seats.get_hold(payment.idempotency_key) if restored else None

            if not hold:
                transitioned = reservation_recovery.transition_pending_status(
                    db,
                    booking,
                    "expired"
                )
                db.commit()
                seat_record = db.query(dbcon.FlightSeat).filter(
                    dbcon.FlightSeat.flight_instance_id == booking.flight_instance_id
                ).first()
                if seat_record:
                    availability = redis_seats.sync_flight_cache(seat_record)
                    redis_seats.publish_seat_update(booking.flight_instance_id, availability)
                raise HTTPException(
                    status_code=400,
                    detail="Reservation expired. Please book again."
                )

        payment_result = run_dummy_payment()

        if not payment_result["success"]:
            transitioned = reservation_recovery.transition_pending_status(
                db,
                booking,
                "payment_failed"
            )
            db.commit()
            if transitioned:
                redis_seats.release_hold(payment.idempotency_key, return_seats=True)
            seat_record = db.query(dbcon.FlightSeat).filter(
                dbcon.FlightSeat.flight_instance_id == booking.flight_instance_id
            ).first()
            if seat_record:
                availability = redis_seats.sync_flight_cache(seat_record)
                redis_seats.publish_seat_update(booking.flight_instance_id, availability)
            return {
                "message": "Payment failed. Reserved seats were released.",
                "booking_id": booking.id,
                "status": booking.status,
                "payment": payment_result
            }

        available_field = f"{booking.travel_class}_available"
        updated_rows = db.query(dbcon.FlightSeat).filter(
            dbcon.FlightSeat.flight_instance_id == booking.flight_instance_id,
            getattr(dbcon.FlightSeat, available_field) >= booking.seats
        ).update(
            {
                getattr(dbcon.FlightSeat, available_field):
                getattr(dbcon.FlightSeat, available_field) - booking.seats
            },
            synchronize_session=False
        )

        if updated_rows == 0:
            reservation_recovery.transition_pending_status(
                db,
                booking,
                "expired"
            )
            db.commit()
            redis_seats.release_hold(payment.idempotency_key, return_seats=True)
            raise HTTPException(
                status_code=400,
                detail="Seats are no longer available. Please book again."
            )

        booking.status = "confirmed"
        log_flight_interaction(
            db,
            current_user,
            {
                "flight_instance_id": booking.flight_instance_id,
                "flight_number": booking.flight_number,
            },
            "booking_confirmed",
            5.0
        )
        db.commit()
        redis_seats.release_hold(payment.idempotency_key, return_seats=False)

        seat_record = db.query(dbcon.FlightSeat).filter(
            dbcon.FlightSeat.flight_instance_id == booking.flight_instance_id
        ).first()
        availability = redis_seats.sync_flight_cache(seat_record)
        redis_seats.publish_seat_update(booking.flight_instance_id, availability)

        return {
            "message": "Payment confirmed. Ticket booked successfully.",
            "booking_id": booking.id,
            "flight_instance_id": booking.flight_instance_id,
            "status": booking.status,
            "payment": payment_result,
            "remaining_seats": availability
        }
    finally:
        db.close()


@app.get("/flight-instances/{flight_instance_id}/seat-events")
async def flight_seat_events(
    flight_instance_id: str,
    request: Request,
    current_user: dbcon.User = Depends(get_current_user)
):
    async def event_stream():
        db = dbcon.SESSION_LOCAL()
        pubsub = redis_seats.redis_client.pubsub()

        try:
            seat_record = db.query(dbcon.FlightSeat).filter(
                dbcon.FlightSeat.flight_instance_id == flight_instance_id
            ).first()

            if seat_record:
                availability = redis_seats.cached_availability(seat_record)
                yield sse_event("seat_update", {
                    "flight_instance_id": flight_instance_id,
                    "flight_number": seat_record.flight_number,
                    "departure_time": seat_record.departure_time,
                    "seat_availability": availability
                })

            pubsub.subscribe(redis_seats.flight_channel(flight_instance_id))

            while not await request.is_disconnected():
                message = pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0
                )

                if message and message.get("data"):
                    yield sse_event("seat_update", json.loads(message["data"]))
                else:
                    yield sse_event("heartbeat", {"flight_instance_id": flight_instance_id})

                await asyncio.sleep(1)
        finally:
            pubsub.close()
            db.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/my-tickets")
async def my_tickets(current_user: dbcon.User = Depends(get_current_user)):
    db = dbcon.SESSION_LOCAL()

    try:
        tickets = db.query(dbcon.Booking).filter(
            dbcon.Booking.user_id == current_user.id,
            dbcon.Booking.status.in_(["confirmed", "pending", "refunded", "payment_failed", "expired"])
        ).order_by(dbcon.Booking.id.desc()).all()

        return {
            "tickets": [serialize_ticket(ticket) for ticket in tickets]
        }
    finally:
        db.close()


@app.post("/my-tickets/{booking_id}/refund")
async def request_refund(
    booking_id: str,
    refund: RefundRequest,
    current_user: dbcon.User = Depends(get_current_user)
):
    reason = refund.reason.strip()

    if not reason:
        raise HTTPException(status_code=400, detail="Refund reason is required")

    db = dbcon.SESSION_LOCAL()

    try:
        booking = db.query(dbcon.Booking).filter(
            dbcon.Booking.id == booking_id,
            dbcon.Booking.user_id == current_user.id
        ).first()

        if not booking:
            raise HTTPException(status_code=404, detail="Ticket not found")

        if booking.status != "confirmed":
            raise HTTPException(
                status_code=400,
                detail="Only confirmed tickets can request a refund"
            )

        if booking.refund_status in {"pending", "approved"}:
            raise HTTPException(
                status_code=400,
                detail=f"Refund is already {booking.refund_status}"
            )

        booking.refund_status = "pending"
        booking.refund_reason = reason
        booking.refund_requested_at = datetime.utcnow()
        booking.refund_admin_note = None
        booking.refund_resolved_at = None
        db.commit()
        db.refresh(booking)

        return {
            "message": "Refund request submitted",
            "ticket": serialize_ticket(booking)
        }
    finally:
        db.close()


@app.get("/admin/refunds")
async def admin_refunds(
    status: str = Query("pending"),
    current_admin: dbcon.User = Depends(get_current_admin)
):
    normalized_status = status.strip().lower()
    allowed_statuses = {"pending", "approved", "rejected", "all"}

    if normalized_status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail="status must be pending, approved, rejected, or all"
        )

    db = dbcon.SESSION_LOCAL()

    try:
        query = db.query(dbcon.Booking).filter(
            dbcon.Booking.refund_status != "none"
        )

        if normalized_status != "all":
            query = query.filter(dbcon.Booking.refund_status == normalized_status)

        refunds = query.order_by(dbcon.Booking.refund_requested_at.desc()).all()

        return {
            "refunds": [serialize_ticket(refund) for refund in refunds]
        }
    finally:
        db.close()


@app.post("/admin/refunds/{booking_id}/decision")
async def decide_refund(
    booking_id: str,
    decision: AdminRefundDecision,
    current_admin: dbcon.User = Depends(get_current_admin)
):
    action = decision.action.strip().lower()

    if action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="action must be approve or reject")

    db = dbcon.SESSION_LOCAL()

    try:
        booking = db.query(dbcon.Booking).filter(
            dbcon.Booking.id == booking_id,
            dbcon.Booking.refund_status == "pending"
        ).first()

        if not booking:
            raise HTTPException(status_code=404, detail="Pending refund not found")

        booking.refund_admin_note = decision.note
        booking.refund_resolved_at = datetime.utcnow()

        if action == "reject":
            booking.refund_status = "rejected"
            db.commit()
            db.refresh(booking)
            return {
                "message": "Refund rejected",
                "ticket": serialize_ticket(booking)
            }

        available_field = f"{booking.travel_class}_available"
        db.query(dbcon.FlightSeat).filter(
            dbcon.FlightSeat.flight_instance_id == booking.flight_instance_id
        ).update(
            {
                getattr(dbcon.FlightSeat, available_field):
                getattr(dbcon.FlightSeat, available_field) + booking.seats
            },
            synchronize_session=False
        )

        booking.refund_status = "approved"
        booking.status = "refunded"
        db.commit()
        db.refresh(booking)

        seat_record = db.query(dbcon.FlightSeat).filter(
            dbcon.FlightSeat.flight_instance_id == booking.flight_instance_id
        ).first()

        if seat_record:
            availability = redis_seats.sync_flight_cache(seat_record)
            redis_seats.publish_seat_update(booking.flight_instance_id, availability)

        return {
            "message": "Refund approved and seats returned",
            "ticket": serialize_ticket(booking)
        }
    finally:
        db.close()


@app.post("/create-itinerary")
async def create_itinerary(
    details: CreateItinerary,
    current_user: dbcon.User = Depends(get_current_user)
):
    if details.days < 1:
        raise HTTPException(
            status_code=400,
            detail="days must be at least 1"
        )

    try:
        result = generate_itinerary(details)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Itinerary generation failed: {exc}"
        )

    return {
        "destination": details.destination,
        "days": details.days,
        "created_for": current_user.username,
        **result
    }


@app.get("/flights")
async def search_route_flights(
    from_city: str = Query(..., alias="from", min_length=1),
    to_city: str = Query(..., alias="to", min_length=1),
    current_user: dbcon.User = Depends(get_current_user)
):
    cache_lock_acquired = False
    try:
        cached_response = redis_seats.get_cached_flight_search(from_city, to_city)
    except RedisError:
        cached_response = None

    if cached_response:
        cached_response = refresh_cached_seat_availability(cached_response)
        cached_response["flights"] = rank_flights_for_user(
            current_user,
            cached_response.get("flights", [])
        )
        return cached_response

    try:
        cache_lock_acquired = redis_seats.acquire_flight_search_lock(from_city, to_city)
        if not cache_lock_acquired:
            cached_response = await wait_for_flight_search_cache(from_city, to_city)
            if cached_response:
                cached_response = refresh_cached_seat_availability(cached_response)
                cached_response["flights"] = rank_flights_for_user(
                    current_user,
                    cached_response.get("flights", [])
                )
                return cached_response
    except RedisError:
        cache_lock_acquired = False

    departure_airport_info = find_airport_for_location(from_city)
    arrival_airport_info = find_airport_for_location(to_city)
    weather_location = arrival_airport_info["city"] or to_city
    latitude = arrival_airport_info["latitude"]
    longitude = arrival_airport_info["longitude"]

    if latitude is None or longitude is None:
        geo_url = (
            f"https://geocoding-api.open-meteo.com/v1/search"
            f"?name={weather_location}&count=1"
        )
        geo = fetch_json(geo_url)

        if not geo or "results" not in geo:
            raise HTTPException(
                status_code=404,
                detail="Destination city or airport not found"
            )

        location = geo["results"][0]
        latitude = location["latitude"]
        longitude = location["longitude"]

    places = fetch_destination_places(latitude, longitude)

    weather_url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}"
        f"&longitude={longitude}"
        f"&current=temperature_2m,"
        f"relative_humidity_2m,"
        f"wind_speed_10m"
        f"&daily=temperature_2m_max,"
        f"temperature_2m_min,"
        f"precipitation_sum,"
        f"wind_speed_10m_max"
        f"&forecast_days=3"
    )
    weather = fetch_json(weather_url)
    current = (weather or {}).get("current")

    if not current:
        raise HTTPException(
            status_code=503,
            detail="Weather service is unavailable. Please try again later."
        )

    weather_forecast = format_weather_forecast(weather)
    departure_code = departure_airport_info["iata"]
    arrival_code = arrival_airport_info["iata"]
    flights = []

    if departure_code and arrival_code and AVIATIONSTACK_API_KEY:
        flight_url = (
            "https://api.aviationstack.com/v1/flights"
            f"?access_key={AVIATIONSTACK_API_KEY}"
            f"&dep_iata={departure_code}"
            f"&arr_iata={arrival_code}"
        )
        flight_data = fetch_json(flight_url)

        if flight_data and "data" in flight_data:
            db = dbcon.SESSION_LOCAL()

            for flight in flight_data["data"][:5]:
                dep_iata = flight["departure"].get("iata")
                arr_iata = flight["arrival"].get("iata")

                if dep_iata != departure_code or arr_iata != arrival_code:
                    continue

                departure_airport = db.query(dbcon.Airport).filter(
                    dbcon.Airport.iata_code == dep_iata
                ).first()
                arrival_airport = db.query(dbcon.Airport).filter(
                    dbcon.Airport.iata_code == arr_iata
                ).first()

                distance = None
                ticket_price = None

                if departure_airport and arrival_airport:
                    distance = calculate_distance(
                        departure_airport.latitude,
                        departure_airport.longitude,
                        arrival_airport.latitude,
                        arrival_airport.longitude
                    )
                    ticket_price = simulate_price(distance)

                flights.append({
                    "flight_number": flight["flight"].get("iata"),
                    "airline": flight["airline"].get("name"),
                    "from": flight["departure"].get("airport"),
                    "to": flight["arrival"].get("airport"),
                    "from_city": from_city.title(),
                    "to_city": to_city.title(),
                    "departure_iata": dep_iata,
                    "arrival_iata": arr_iata,
                    "distance_km": round(distance, 2) if distance else None,
                    "departure_time": flight["departure"].get("scheduled"),
                    "arrival_time": flight["arrival"].get("scheduled"),
                    "status": flight.get("flight_status"),
                    "source": "aviationstack"
                })
                flights[-1] = add_booking_options(flights[-1], ticket_price)

            db.close()

    if len(flights) < 5:
        local_departure = find_local_airport(from_city)
        local_arrival = find_local_airport(to_city)
        fallback_flights = build_route_fallback_flight(
            from_city,
            to_city,
            local_departure,
            local_arrival
        )
        existing_instances = {
            flight.get("flight_instance_id")
            for flight in flights
        }
        for fallback_flight in fallback_flights:
            if fallback_flight.get("flight_instance_id") in existing_instances:
                continue
            flights.append(fallback_flight)
            existing_instances.add(fallback_flight.get("flight_instance_id"))
            if len(flights) == 5:
                break

    flights = rank_flights_for_user(current_user, flights)
    log_search_impressions(current_user, flights)

    db = dbcon.SESSION_LOCAL()
    history = dbcon.WeatherHistory(
        city=to_city.title(),
        temperature=current["temperature_2m"],
        humidity=current["relative_humidity_2m"],
        wind_speed=current["wind_speed_10m"],
        user_id=current_user.id
    )
    db.add(history)
    db.commit()
    db.close()

    response_payload = {
        "from": from_city.title(),
        "destination": to_city.title(),
        "departure_airport": departure_airport_info["name"],
        "arrival_airport": arrival_airport_info["name"],
        "airport": arrival_airport_info["name"],
        "weather": {
            "temperature": current["temperature_2m"],
            "humidity": current["relative_humidity_2m"],
            "wind_speed": current["wind_speed_10m"]
        },
        "weather_forecast": weather_forecast,
        "flights": flights,
        "places": places,
        "cache": "miss"
    }

    try:
        redis_seats.set_cached_flight_search(
            from_city,
            to_city,
            flight_cache_payload(response_payload)
        )
    except RedisError:
        pass

    if cache_lock_acquired:
        try:
            redis_seats.release_flight_search_lock(from_city, to_city)
        except RedisError:
            pass

    return response_payload


@app.get("/flights/{city}")
async def search_flights(
    city: str,
    current_user: dbcon.User = Depends(get_current_user)
):
    cache_lock_acquired = False
    try:
        cached_response = redis_seats.get_cached_flight_search(city, "__legacy__")
    except RedisError:
        cached_response = None

    if cached_response:
        return refresh_cached_seat_availability(cached_response)

    try:
        cache_lock_acquired = redis_seats.acquire_flight_search_lock(city, "__legacy__")
        if not cache_lock_acquired:
            cached_response = await wait_for_flight_search_cache(city, "__legacy__")
            if cached_response:
                return refresh_cached_seat_availability(cached_response)
    except RedisError:
        cache_lock_acquired = False

    # Get city coordinates
   
    geo_url = (
        f"https://geocoding-api.open-meteo.com/v1/search"
        f"?name={city}&count=1"
    )

    geo = fetch_json(geo_url)

    if not geo or "results" not in geo:
        raise HTTPException(
            status_code=404,
            detail="City not found"
        )

    location = geo["results"][0]

    latitude = location["latitude"]
    longitude = location["longitude"]
    places = fetch_destination_places(latitude, longitude)
    



    # Weather details fetching
  
    weather_url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}"
        f"&longitude={longitude}"
        f"&current=temperature_2m,"
        f"relative_humidity_2m,"
        f"wind_speed_10m"
        f"&daily=temperature_2m_max,"
        f"temperature_2m_min,"
        f"precipitation_sum,"
        f"wind_speed_10m_max"
        f"&forecast_days=3"
    )

    weather = fetch_json(weather_url)

    current = (weather or {}).get("current")

    if not current:
        raise HTTPException(
            status_code=503,
            detail="Weather service is unavailable. Please try again later."
        )

    weather_forecast = format_weather_forecast(weather)


    # Find Airport data
    
    local_airport = None
    airport_data = None

    if AVIATIONSTACK_API_KEY:
        airport_url = (
            "https://api.aviationstack.com/v1/airports"
            f"?access_key={AVIATIONSTACK_API_KEY}"
            f"&search={city}"
        )

        airport_data = fetch_json(airport_url)

    if (
        not airport_data
        or "data" not in airport_data
        or len(airport_data["data"]) == 0
    ):
        local_airport = find_local_airport(city)
        airport = local_airport.airport_name if local_airport else None
        airport_code = local_airport.iata_code if local_airport else None

    else:
        airport = airport_data["data"][0]["airport_name"]
        airport_code = airport_data["data"][0]["iata_code"]

   
    # Flight Information code section
   
    flights = []

    if airport_code and AVIATIONSTACK_API_KEY:

        flight_url = (
            "https://api.aviationstack.com/v1/flights"
            f"?access_key={AVIATIONSTACK_API_KEY}"
            f"&dep_iata={airport_code}"
        )

        flight_data = fetch_json(flight_url)

        if flight_data and "data" in flight_data:

            db = dbcon.SESSION_LOCAL()

            for flight in flight_data["data"][:5]:

                dep_iata = flight["departure"].get("iata")
                arr_iata = flight["arrival"].get("iata")

                if not dep_iata or not arr_iata:
                    continue

                departure_airport = db.query(dbcon.Airport).filter(
                    dbcon.Airport.iata_code == dep_iata
                ).first()

                arrival_airport = db.query(dbcon.Airport).filter(
                    dbcon.Airport.iata_code == arr_iata
                ).first()

                distance = None
                ticket_price = None

                if departure_airport and arrival_airport:

                    distance = calculate_distance(
                        departure_airport.latitude,
                        departure_airport.longitude,
                        arrival_airport.latitude,
                        arrival_airport.longitude
                    )

                    ticket_price = simulate_price(distance)

                flights.append({

                    "flight_number":
                        flight["flight"].get("iata"),

                    "airline":
                        flight["airline"].get("name"),

                    "from":
                        flight["departure"].get("airport"),

                    "to":
                        flight["arrival"].get("airport"),

                    "departure_iata":
                        dep_iata,

                    "arrival_iata":
                        arr_iata,

                    "distance_km":
                        round(distance, 2) if distance else None,

                    "departure_time":
                        flight["departure"].get("scheduled"),

                    "arrival_time":
                        flight["arrival"].get("scheduled"),

                    "status":
                        flight.get("flight_status"),

                    "ticket_price":
                        f"₹{ticket_price:,}" if ticket_price else "Unavailable"

                })

                flights[-1] = add_booking_options(flights[-1], ticket_price)

            db.close()

    if not flights and airport_code:
        fallback_airport = local_airport or find_local_airport(city)

        if fallback_airport:
            flights = build_fallback_flights(fallback_airport)

    

    db = dbcon.SESSION_LOCAL()

    history = dbcon.WeatherHistory(
        city=city.title(),
        temperature=current["temperature_2m"],
        humidity=current["relative_humidity_2m"],
        wind_speed=current["wind_speed_10m"],
        user_id=current_user.id
    )

    db.add(history)
    db.commit()
    db.close()

   
    # Response with weather and flight data
    

    response_payload = {

        "destination": city.title(),

        "airport": airport,

        "weather": {

            "temperature":
                current["temperature_2m"],

            "humidity":
                current["relative_humidity_2m"],

            "wind_speed":
                current["wind_speed_10m"]

        },

        "weather_forecast": weather_forecast,

        "flights": flights,

        "places": places,

        "cache": "miss"

    }

    try:
        redis_seats.set_cached_flight_search(
            city,
            "__legacy__",
            flight_cache_payload(response_payload)
        )
    except RedisError:
        pass

    if cache_lock_acquired:
        try:
            redis_seats.release_flight_search_lock(city, "__legacy__")
        except RedisError:
            pass

    return response_payload


def run_search_sync(from_city: str, to_city: str, current_user: dbcon.User):
    try:
        cached_response = redis_seats.get_cached_flight_search(from_city, to_city)
    except RedisError:
        cached_response = None

    if cached_response:
        cached_response = refresh_cached_seat_availability(cached_response)
        cached_response["flights"] = rank_flights_for_user(
            current_user,
            cached_response.get("flights", [])
        )
        return cached_response

    departure_airport_info = find_airport_for_location(from_city)
    arrival_airport_info = find_airport_for_location(to_city)
    weather_location = arrival_airport_info["city"] or to_city
    latitude = arrival_airport_info["latitude"]
    longitude = arrival_airport_info["longitude"]

    if latitude is None or longitude is None:
        geo_url = (
            f"https://geocoding-api.open-meteo.com/v1/search"
            f"?name={weather_location}&count=1"
        )
        geo = fetch_json(geo_url)
        if geo and "results" in geo and geo["results"]:
            location = geo["results"][0]
            latitude = location["latitude"]
            longitude = location["longitude"]

    places = fetch_destination_places(latitude, longitude) if (latitude and longitude) else {"status": "unavailable"}

    weather = None
    if latitude and longitude:
        weather_url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={latitude}"
            f"&longitude={longitude}"
            f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
            f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max"
            f"&forecast_days=3"
        )
        weather = fetch_json(weather_url)

    current = (weather or {}).get("current", {"temperature_2m": 22.0, "relative_humidity_2m": 50, "wind_speed_10m": 10.0})
    weather_forecast = format_weather_forecast(weather) if weather else []

    local_departure = find_local_airport(from_city)
    local_arrival = find_local_airport(to_city)
    flights = build_route_fallback_flight(
        from_city,
        to_city,
        local_departure,
        local_arrival
    )

    flights = rank_flights_for_user(current_user, flights)

    response_payload = {
        "from": from_city.title(),
        "destination": to_city.title(),
        "departure_airport": departure_airport_info.get("name", from_city.title()),
        "arrival_airport": arrival_airport_info.get("name", to_city.title()),
        "weather": {
            "temperature": current.get("temperature_2m", 22),
            "humidity": current.get("relative_humidity_2m", 50),
            "wind_speed": current.get("wind_speed_10m", 10)
        },
        "weather_forecast": weather_forecast,
        "flights": flights,
        "places": places,
    }

    try:
        redis_seats.set_cached_flight_search(
            from_city,
            to_city,
            flight_cache_payload(response_payload)
        )
    except RedisError:
        pass

    return response_payload


def run_book_sync(data: dict, current_user: dbcon.User):
    flight_instance_id = data["flight_instance_id"]
    travel_class = normalize_travel_class(data.get("travel_class", "economy"))
    seats = int(data.get("seats", 1))
    idempotency_key = data.get("idempotency_key") or str(uuid.uuid4())
    departure_time = data.get("departure_time")

    if seats < 1:
        raise HTTPException(status_code=400, detail="seats must be at least 1")

    db = dbcon.SESSION_LOCAL()
    try:
        seat_record = db.query(dbcon.FlightSeat).filter(
            dbcon.FlightSeat.flight_instance_id == flight_instance_id
        ).first()

        if not seat_record:
            flight_num = flight_instance_id.split("-")[0] if "-" in flight_instance_id else "FL-101"
            simulated_econ = simulate_price(1200)
            simulated_biz = round(simulated_econ * CLASS_MULTIPLIERS.get("business", 1.8))
            seat_record = dbcon.FlightSeat(
                flight_instance_id=flight_instance_id,
                flight_number=flight_num,
                departure_time=departure_time or datetime.utcnow().isoformat(),
                economy_total=160,
                economy_available=160,
                economy_price=float(simulated_econ),
                business_total=30,
                business_available=30,
                business_price=float(simulated_biz),
                first_total=10,
                first_available=10,
                first_price=float(simulated_biz * 1.5)
            )
            db.add(seat_record)
            db.commit()
            db.refresh(seat_record)

        price_field = f"{travel_class}_price"
        price = getattr(seat_record, price_field) or simulate_price(1000)

        try:
            reserved, remaining_seats, expires_at = redis_seats.reserve_seats(
                seat_record,
                travel_class,
                seats,
                idempotency_key
            )
        except RedisError:
            # Fallback if redis unavailable
            expires_at = int(datetime.utcnow().timestamp()) + 600
            reserved = True
            remaining_seats = getattr(seat_record, f"{travel_class}_available") - seats

        if not reserved:
            raise HTTPException(
                status_code=400,
                detail=f"Only {remaining_seats} {travel_class} seats available"
            )

        amount = price * seats
        order_id = new_payment_order_id()
        reservation_expires_at = datetime.utcfromtimestamp(expires_at)

        from_city = data.get("from_city") or getattr(seat_record, "from_city", None)
        to_city = data.get("to_city") or getattr(seat_record, "to_city", None)

        if not from_city or not to_city:
            from_c, to_c = resolve_ticket_route(db, seat_record)
            from_city = from_city or from_c
            to_city = to_city or to_c

        booking = dbcon.Booking(
            flight_instance_id=seat_record.flight_instance_id,
            flight_number=seat_record.flight_number,
            departure_time=seat_record.departure_time or departure_time or datetime.utcnow().isoformat(),
            from_city=from_city,
            to_city=to_city,
            travel_class=travel_class,
            seats=seats,
            amount=amount,
            payment_order_id=order_id,
            idempotency_key=idempotency_key,
            status="pending",
            reservation_expires_at=reservation_expires_at,
            user_id=current_user.id
        )

        db.add(booking)
        db.commit()
        db.refresh(booking)

        payment_session = create_dummy_payment_session(amount, booking, idempotency_key)

        return {
            "message": "Seats reserved. Complete payment before the reservation expires.",
            "booking_id": booking.id,
            "flight_instance_id": booking.flight_instance_id,
            "flight_number": booking.flight_number,
            "departure_time": booking.departure_time,
            "travel_class": travel_class,
            "seats_reserved": seats,
            "status": booking.status,
            "reservation_expires_at": reservation_expires_at.isoformat(),
            "amount": amount,
            "payment_session": payment_session
        }
    finally:
        db.close()


@app.post("/agent/chat")
def agent_chat_endpoint(
    req: AgentChatRequest,
    current_user: dbcon.User = Depends(get_current_user)
):
    result = run_flight_agent(
        user_message=req.message,
        current_user=current_user,
        search_fn=run_search_sync,
        book_fn=run_book_sync,
        chat_history=req.chat_history
    )
    return result
