
from pydantic import BaseModel

class Signup(BaseModel):
    username: str
    email: str
    password: str


class Login(BaseModel):
    username: str
    password: str


class BookTicket(BaseModel):
    flight_instance_id: str
    flight_number: str
    departure_time: str | None = None
    travel_class: str
    seats: int = 1
    idempotency_key: str


class PaymentResult(BaseModel):
    idempotency_key: str
    action: str = "complete"
    success: bool | None = None


class RefundRequest(BaseModel):
    reason: str


class AdminRefundDecision(BaseModel):
    action: str
    note: str | None = None


class CreateItinerary(BaseModel):
    destination: str
    airport: str | None = None
    weather: dict | None = None
    flights: list[dict] = []
    places: dict | None = None
    days: int = 3
