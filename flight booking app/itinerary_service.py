import os

import requests


REQUEST_TIMEOUT_SECONDS = 25
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def build_itinerary_prompt(details):
    return f"""
Create a practical travel itinerary using only the available trip details.

Destination: {details.destination}
Airport: {details.airport}
Weather: {details.weather}
Flights: {details.flights}
Places, hotels, and restaurants: {details.places}
Days: {details.days}

Return a complete day-by-day itinerary with morning, afternoon, evening,
hotel suggestions, food suggestions, travel tips, and a short budget note.
Keep it useful for a real traveler.
"""


def call_gemini(prompt):
    api_key = os.getenv("GEMINI_API_KEY", "")

    if not api_key:
        return None, "Gemini API key is not configured"

    try:
        response = requests.post(
            f"{GEMINI_URL}?key={api_key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return text, None
    except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
        return None, str(exc)


def call_groq(prompt):
    api_key = os.getenv("GROQ_API_KEY", "")

    if not api_key:
        return None, "Groq API key is not configured"

    try:
        response = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a helpful travel itinerary planner."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "temperature": 0.6
            },
            timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"], None
    except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
        return None, str(exc)


def local_itinerary_fallback(details, errors):
    places = details.places or {}
    tourist_spots = places.get("tourist_spots", [])[:5]
    hotels = places.get("hotels", [])[:3]
    restaurants = places.get("restaurants", [])[:3]

    spot_names = ", ".join(place.get("name", "Nearby attraction") for place in tourist_spots)
    hotel_names = ", ".join(place.get("name", "Nearby hotel") for place in hotels)
    restaurant_names = ", ".join(place.get("name", "Local restaurant") for place in restaurants)

    return {
        "provider": "local_fallback",
        "errors": errors,
        "itinerary": (
            f"{details.days}-day itinerary for {details.destination}\n\n"
            f"Stay options: {hotel_names or 'Use the hotels list from OpenStreetMap.'}\n"
            f"Food options: {restaurant_names or 'Try well-rated local restaurants near your stay.'}\n"
            f"Main places: {spot_names or 'Explore central attractions and parks nearby.'}\n\n"
            "Day 1: Arrive, check in, explore nearby food spots, and keep the evening light.\n"
            "Day 2: Visit the top tourist spots in the morning and afternoon, then plan dinner nearby.\n"
            "Day 3: Use the morning for parks, viewpoints, shopping, or museums before departure.\n\n"
            "Tip: Confirm opening hours, local transport, and weather before leaving each day."
        )
    }


def generate_itinerary(details):
    prompt = build_itinerary_prompt(details)
    errors = {}

    itinerary, error = call_groq(prompt)
    if itinerary:
        return {
            "provider": "groq",
            "itinerary": itinerary
        }
    errors["groq"] = error

    itinerary, error = call_gemini(prompt)
    if itinerary:
        return {
            "provider": "gemini",
            "itinerary": itinerary,
            "fallback_from": "groq"
        }
    errors["gemini"] = error

    return local_itinerary_fallback(details, errors)

