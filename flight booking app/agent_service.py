import os
import json
import uuid
import logging
import re
from typing import Annotated, Sequence, TypedDict, Any, Dict, List, Optional
import urllib.request
import urllib.error

INJECTION_PATTERNS = [
    r"forget (all|your|previous|the) (system prompt|instructions|rules|guidelines)",
    r"ignore (all|your|previous|the) (system prompt|instructions|rules|guidelines)",
    r"i am (the )?(root|admin|system|developer|creator) user",
    r"you are now",
    r"dan mode",
    r"jailbreak",
    r"system override",
    r"override (system|prompt|rules)",
    r"write a (python|javascript|code|script|program|c\+\+|java)",
    r"print the sum",
    r"calculate the sum",
    r"write code to",
    r"solve (this|the) math",
]

def is_prompt_injection_or_off_topic(user_input: str) -> tuple[bool, str]:
    text_lower = user_input.lower().strip()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text_lower):
            return True, (
                "I am SkyNav AI, specialized strictly as your flight concierge and airline FAQ assistant. "
                "I cannot assist with programming, general coding tasks, or requests outside of flight and travel operations."
            )
    return False, ""

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

import dbcon
import redis_seats
from recommender_service import rank_flights_for_user
from payment_service import create_dummy_payment_session, new_payment_order_id
from datetime import datetime

logger = logging.getLogger(__name__)

# State schema for LangGraph multi-agent system
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    user_id: int
    username: str
    pending_booking: Optional[Dict[str, Any]]
    search_results: Optional[Dict[str, Any]]
    next_agent: Optional[str]


def get_groq_llm():
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from langchain_groq import ChatGroq
        model_name = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
        return ChatGroq(
            model=model_name,
            groq_api_key=api_key,
            temperature=0.2,
            max_retries=2,
        )
    except Exception as e:
        logger.warning(f"Groq LLM initialization failed: {e}")
        return None


def get_gemini_llm():
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            temperature=0.2,
            max_output_tokens=2048,
        )
    except Exception as e:
        logger.warning(f"Gemini LLM initialization failed: {e}")
        return None


def get_agent_llm(tools: Optional[List[Any]] = None):
    """
    Returns an LLM instance configured with Groq as primary and Gemini as fallback.
    If Groq fails or is not configured, it seamlessly falls back to Gemini.
    """
    groq_llm = get_groq_llm()
    gemini_llm = get_gemini_llm()

    if tools:
        if groq_llm:
            groq_llm = groq_llm.bind_tools(tools)
        if gemini_llm:
            gemini_llm = gemini_llm.bind_tools(tools)

    if groq_llm and gemini_llm:
        return groq_llm.with_fallbacks([gemini_llm])
    elif groq_llm:
        return groq_llm
    elif gemini_llm:
        return gemini_llm
    else:
        raise ValueError(
            "Neither GROQ_API_KEY nor GEMINI_API_KEY is configured in your environment. "
            "Please set at least GROQ_API_KEY or GEMINI_API_KEY in your .env file."
        )



import contextvars

# Context object using contextvars to guarantee thread-safe and task-isolated state per user request
_agent_context_var: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar("agent_context", default={})

def set_agent_context(user: dbcon.User, search_fn, book_fn) -> Dict[str, Any]:
    ctx = {
        "user": user,
        "search_fn": search_fn,
        "book_fn": book_fn,
        "pending_booking": None,
        "last_search": None
    }
    _agent_context_var.set(ctx)
    return ctx

def get_agent_context() -> Dict[str, Any]:
    return _agent_context_var.get()


# ---------------------------------------------------------------------------
# FLIGHT BOOKING SUB-AGENT TOOLS
# ---------------------------------------------------------------------------

@tool
def search_flights_tool(from_city: str, to_city: str) -> str:
    """
    Search for available flights between two cities or airports.
    Returns flight options, prices, seat availability, weather, and recommendations.
    Use this tool whenever the user asks about flights between cities (e.g. from New York to London).
    """
    ctx = get_agent_context()
    user = ctx.get("user")
    search_fn = ctx.get("search_fn")
    
    if not search_fn or not user:
        return "Error: Search service context is missing."

    try:
        result = search_fn(from_city, to_city, user)
        ctx["last_search"] = result
        
        flights = result.get("flights", [])
        if not flights:
            return f"No flights found from {from_city} to {to_city}."
        
        output = [
            f"Found {len(flights)} flight(s) from {result.get('from')} to {result.get('destination')}:",
            f"Arrival Airport: {result.get('arrival_airport')}",
            f"Weather at destination: {result.get('weather', {}).get('temperature')}°C"
        ]
        
        for idx, f in enumerate(flights, 1):
            prices = f.get("ticket_price", {})
            seats = f.get("seat_availability", {})
            rec = f.get("recommendation", {})
            rec_tag = "[RECOMMENDED]" if rec.get("is_recommended") else ""
            
            output.append(
                f"\n{idx}. Flight {f.get('flight_number')} ({f.get('airline', 'Airline')}) {rec_tag}\n"
                f"   Instance ID: {f.get('flight_instance_id')}\n"
                f"   Departure: {f.get('departure_time', 'Scheduled')} | Arrival: {f.get('arrival_time', 'Scheduled')}\n"
                f"   Prices: Economy ₹{prices.get('economy', 'N/A')}, Business ₹{prices.get('business', 'N/A')}\n"
                f"   Seats Available: Economy: {seats.get('economy', 0)}, Business: {seats.get('business', 0)}\n"
                f"   Recommendation Score: {rec.get('score')} ({rec.get('reason')})"
            )
        
        return "\n".join(output)
    except Exception as e:
        logger.error(f"Search flight tool error: {e}", exc_info=True)
        return f"Failed to search flights from {from_city} to {to_city}: {str(e)}"


@tool
def recommend_flight_tool(from_city: str, to_city: str, travel_class: str = "economy") -> str:
    """
    Evaluate and recommend the single best flight for a route based on AI recommendation scores, user preferences, and seat availability.
    """
    ctx = get_agent_context()
    user = ctx.get("user")
    search_fn = ctx.get("search_fn")
    
    if not search_fn or not user:
        return "Error: Search service context is missing."

    try:
        search_result = search_fn(from_city, to_city, user)
        flights = search_result.get("flights", [])
        if not flights:
            return f"No flights found to recommend between {from_city} and {to_city}."
        
        best_flight = flights[0]
        prices = best_flight.get("ticket_price", {})
        seats = best_flight.get("seat_availability", {})
        rec = best_flight.get("recommendation", {})
        
        return (
            f"Top Recommended Flight:\n"
            f"Flight: {best_flight.get('flight_number')} with {best_flight.get('airline')}\n"
            f"Flight Instance ID: {best_flight.get('flight_instance_id')}\n"
            f"Class: {travel_class.capitalize()} (₹{prices.get(travel_class.lower(), 'N/A')})\n"
            f"Available Seats: {seats.get(travel_class.lower(), 0)}\n"
            f"Why Recommended: {rec.get('reason', 'Best price and preference match')}\n"
            f"Departure Time: {best_flight.get('departure_time')}"
        )
    except Exception as e:
        return f"Failed to recommend flight: {str(e)}"


@tool
def reserve_flight_booking_tool(
    flight_instance_id: str,
    travel_class: str = "economy",
    seats: int = 1,
    departure_time: str = "",
    from_city: str = "",
    to_city: str = ""
) -> str:
    """
    Reserve seats and create a pending flight booking.
    THIS TOOL RESERVES SEATS AND PLACES BOOKING IN PENDING STATUS. IT DOES NOT PROCESS PAYMENT.
    Always call this when the user requests to book a flight!
    Parameters:
    - flight_instance_id: The unique flight instance ID (e.g. FL-101-2026-08-20)
    - travel_class: 'economy', 'business', or 'first'
    - seats: Number of seats (integer >= 1)
    - departure_time: Departure timestamp if available
    - from_city: Origin city name (e.g. New York)
    - to_city: Destination city name (e.g. London)
    """
    ctx = get_agent_context()
    user = ctx.get("user")
    book_fn = ctx.get("book_fn")
    last_search = ctx.get("last_search") or {}
    
    if not book_fn or not user:
        return "Error: Booking service context is missing."

    try:
        idempotency_key = str(uuid.uuid4())
        booking_data = {
            "flight_instance_id": flight_instance_id,
            "travel_class": travel_class.lower(),
            "seats": seats,
            "idempotency_key": idempotency_key,
            "departure_time": departure_time,
            "from_city": from_city or last_search.get("from"),
            "to_city": to_city or last_search.get("destination")
        }
        
        res = book_fn(booking_data, user)
        ctx["pending_booking"] = res
        
        return (
            f"SEATS SUCCESSFULLY RESERVED!\n"
            f"Booking ID: {res.get('booking_id')}\n"
            f"Flight Instance ID: {res.get('flight_instance_id')}\n"
            f"Flight Number: {res.get('flight_number')}\n"
            f"Class: {res.get('travel_class')}\n"
            f"Seats Reserved: {res.get('seats_reserved')}\n"
            f"Total Amount: ₹{res.get('amount')}\n"
            f"Status: {res.get('status')} (Awaiting Payment Confirmation)\n"
            f"Reservation Expires At: {res.get('reservation_expires_at')}\n"
            f"Order ID: {res.get('payment_session', {}).get('payment_order_id')}\n"
            f"Idempotency Key: {idempotency_key}\n\n"
            f"IMPORTANT: The booking is held in PENDING status. Instruct the user to review and click 'Confirm Payment' in the Payment Confirmation Card to complete the transaction."
        )
    except Exception as e:
        logger.error(f"Reserve flight tool error: {e}", exc_info=True)
        return f"Failed to reserve flight: {str(e)}"


# ---------------------------------------------------------------------------
# FAQ SUB-AGENT TOOL (FORMDOCK INTEGRATION)
# ---------------------------------------------------------------------------

@tool
def faq_api_tool(question: str) -> str:
    """
    Query the official Formdock FAQ service for answers regarding airline policies, baggage limits,
    cancellations, refund policies, check-in procedures, flight amenities, and general FAQs.
    Parameters:
    - question: The user's question about airline policies or FAQs.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    faq_api_key = os.getenv("FAQ_API_KEY", "").strip()
    base_url = os.getenv("FAQ_BASE_URL", "https://api.formdock.in").strip().rstrip("/")
    endpoint = f"{base_url}/api/faq/chat/"

    if not faq_api_key or faq_api_key == "your_formdock_faq_api_key_here":
        return (
            "FAQ API Key is not configured. Please set a valid FAQ_API_KEY in your .env file "
            "to enable automated Formdock FAQ and policy lookups."
        )

    page_url = os.getenv("FAQ_PAGE_URL", "http://localhost:5173").strip()

    payload = {
        "faq_api_key": faq_api_key,
        "question": question,
        "include_collections": True,
        "metadata": {
            "page_url": page_url,
            "referrer": "",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
    }

    try:
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data_bytes,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            resp_body = resp.read().decode("utf-8")
            res_data = json.loads(resp_body)
            if res_data.get("success") is False or "error" in res_data:
                err_msg = res_data.get("error") or "Unknown FAQ service error"
                if "model_not_found" in str(err_msg) or "llama-3.1-8b-instant" in str(err_msg):
                    return (
                        "Formdock Knowledgebase Error: The Formdock project configuration is using model 'llama-3.1-8b-instant' on Groq, "
                        "which is deprecated or missing access in Formdock dashboard. Please update the LLM model setting in your Formdock dashboard."
                    )
                return f"Formdock FAQ Service Error: {err_msg}"
            
            answer = res_data.get("answer") or "No detailed answer returned from FAQ service."
            return f"FAQ Answer from Formdock Knowledgebase:\n{answer}"
    except urllib.error.HTTPError as http_err:
        logger.error(f"FAQ API HTTP Error {http_err.code}: {http_err.reason}", exc_info=True)
        try:
            err_body = http_err.read().decode("utf-8")
            err_json = json.loads(err_body)
            err_msg = err_json.get("error") or err_json.get("message") or http_err.reason
            if "model_not_found" in str(err_msg) or "llama-3.1-8b-instant" in str(err_msg):
                return (
                    "Formdock Knowledgebase Error: The Formdock project configuration is using model 'llama-3.1-8b-instant' on Groq, "
                    "which is deprecated or missing access in Formdock dashboard. Please update the LLM model setting in your Formdock project dashboard."
                )
            return f"Formdock FAQ Service returned HTTP {http_err.code}: {err_msg}"
        except Exception:
            return f"Formdock FAQ Service returned HTTP {http_err.code}: {http_err.reason}"
    except Exception as e:
        logger.error(f"FAQ API tool error: {e}", exc_info=True)
        return f"Failed to query Formdock FAQ service: {str(e)}"


# ---------------------------------------------------------------------------
# SUB-AGENT SYSTEM PROMPTS & MULTI-AGENT GRAPH ARCHITECTURE
# ---------------------------------------------------------------------------

SUPERVISOR_SYSTEM_PROMPT = """You are the Supervisor Orchestrator for SkyNav AI.
Your sole job is to evaluate incoming user messages and determine which specialized Sub-Agent should handle the request.

Available Sub-Agents:
1. `flight_booking_subagent`: For flight searches between cities, flight price comparisons, recommendations, reserving seats, holding flight bookings, and ticket inquiries.
2. `faq_subagent`: For questions about airline policies, baggage allowances, cancellation fees, refund rules, check-in procedures, flight amenities, or general FAQs.

Rules:
- If the user asks about flights, prices, routes, or booking tickets -> Route to `flight_booking_subagent`.
- If the user asks about policies, baggage, refunds, cancellations, check-in, or general travel FAQs -> Route to `faq_subagent`.
- Respond ONLY with a JSON object: {"next": "flight_booking_subagent"} or {"next": "faq_subagent"}.
"""

FLIGHT_BOOKING_SYSTEM_PROMPT = """You are the Flight Booking Sub-Agent for SkyNav AI.
You are specialized exclusively in flight discovery, preference recommendations, seat holds, and pending booking creation.

Available Tools:
1. `search_flights_tool`: Search flights between cities/airports.
2. `recommend_flight_tool`: Evaluate optimal flights based on user preferences and prices.
3. `reserve_flight_booking_tool`: Reserve seats and create a pending booking record.

Directives:
- All ticket prices MUST be formatted in Indian Rupees (₹).
- When a user asks to book a flight:
  1. Search for flights on the route if not already searched.
  2. Call `reserve_flight_booking_tool` to hold seats and place the booking in PENDING status.
  3. Inform the user clearly that their seats are held and they must click "Confirm Payment" in the Payment Confirmation Card to complete booking.
- Be polite, accurate, and professional.
"""

FAQ_SYSTEM_PROMPT = """You are the Airline FAQ Sub-Agent for SkyNav AI.
You are specialized in answering user questions about airline policies, baggage allowances, cancellation fees, refund rules, check-in procedures, and general travel FAQs.

Available Tools:
1. `faq_api_tool`: Query the official Formdock FAQ database for authoritative policy answers.

Directives:
- Always call `faq_api_tool` when the user asks a question about policies, baggage, refunds, cancellations, or FAQs.
- Present the returned FAQ knowledgebase answer clearly and concisely to the user.
- If the tool indicates the `FAQ_API_KEY` is missing, inform the user to configure `FAQ_API_KEY` in `.env`.
"""


def build_multi_agent_graph():
    flight_tools = [search_flights_tool, recommend_flight_tool, reserve_flight_booking_tool]
    faq_tools = [faq_api_tool]

    flight_tool_node = ToolNode(flight_tools)
    faq_tool_node = ToolNode(faq_tools)

    def supervisor_node(state: AgentState):
        messages = state["messages"]
        user_message = ""
        for m in reversed(messages):
            if isinstance(m, HumanMessage) and m.content:
                user_message = m.content
                break

        # Check prompt injection / off-topic safety guardrail first
        is_injection, refusal_message = is_prompt_injection_or_off_topic(user_message)
        if is_injection:
            return {"next_agent": "end", "messages": [AIMessage(content=refusal_message)]}

        llm = get_agent_llm()
        response = llm.invoke([
            SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT),
            HumanMessage(content=f"Classify this request: '{user_message}'")
        ])

        target = "flight_booking_subagent"
        content = response.content.strip()
        try:
            # Parse JSON decision
            if "{" in content and "}" in content:
                json_str = content[content.find("{"):content.rfind("}")+1]
                parsed = json.loads(json_str)
                target = parsed.get("next", "flight_booking_subagent")
            elif "faq" in content.lower():
                target = "faq_subagent"
        except Exception:
            if "faq" in content.lower():
                target = "faq_subagent"

        return {"next_agent": target}

    def flight_booking_agent_node(state: AgentState):
        messages = state["messages"]
        if not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=FLIGHT_BOOKING_SYSTEM_PROMPT)] + list(messages)
        else:
            messages = [SystemMessage(content=FLIGHT_BOOKING_SYSTEM_PROMPT)] + list(messages[1:])
            
        llm = get_agent_llm(flight_tools)
        response = llm.invoke(messages)
        return {"messages": [response]}

    def faq_agent_node(state: AgentState):
        messages = state["messages"]
        if not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=FAQ_SYSTEM_PROMPT)] + list(messages)
        else:
            messages = [SystemMessage(content=FAQ_SYSTEM_PROMPT)] + list(messages[1:])
            
        llm = get_agent_llm(faq_tools)
        response = llm.invoke(messages)
        return {"messages": [response]}


    def should_continue_flight(state: AgentState):
        messages = state["messages"]
        last_message = messages[-1]
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "flight_tools"
        return END

    def should_continue_faq(state: AgentState):
        messages = state["messages"]
        last_message = messages[-1]
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "faq_tools"
        return END

    def route_supervisor(state: AgentState):
        target = state.get("next_agent", "flight_booking_subagent")
        if target == "end":
            return END
        if target == "faq_subagent":
            return "faq_subagent"
        return "flight_booking_subagent"

    workflow = StateGraph(AgentState)
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("flight_booking_subagent", flight_booking_agent_node)
    workflow.add_node("flight_tools", flight_tool_node)
    workflow.add_node("faq_subagent", faq_agent_node)
    workflow.add_node("faq_tools", faq_tool_node)

    workflow.add_edge(START, "supervisor")
    workflow.add_conditional_edges(
        "supervisor",
        route_supervisor,
        ["flight_booking_subagent", "faq_subagent", END]
    )

    workflow.add_conditional_edges(
        "flight_booking_subagent",
        should_continue_flight,
        ["flight_tools", END]
    )
    workflow.add_edge("flight_tools", "flight_booking_subagent")

    workflow.add_conditional_edges(
        "faq_subagent",
        should_continue_faq,
        ["faq_tools", END]
    )
    workflow.add_edge("faq_tools", "faq_subagent")

    return workflow.compile()


def run_flight_agent(
    user_message: str,
    current_user: dbcon.User,
    search_fn,
    book_fn,
    chat_history: Optional[List[Dict[str, str]]] = None
) -> Dict[str, Any]:
    """
    Executes the Multi-Agent SkyNav AI system for a user message.
    """
    ctx = set_agent_context(current_user, search_fn, book_fn)

    messages: List[BaseMessage] = []

    # Add historical messages if provided
    if chat_history:
        for msg in chat_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role in ("assistant", "agent"):
                messages.append(AIMessage(content=content))

    messages.append(HumanMessage(content=user_message))

    graph = build_multi_agent_graph()
    initial_state = {
        "messages": messages,
        "user_id": current_user.id,
        "username": current_user.username,
        "pending_booking": None,
        "search_results": None,
        "next_agent": None
    }

    try:
        final_state = graph.invoke(initial_state)
        final_messages = final_state.get("messages", [])
        
        last_ai_message = ""
        for m in reversed(final_messages):
            if isinstance(m, AIMessage) and m.content:
                last_ai_message = m.content
                break

        if not last_ai_message:
            last_ai_message = "I have processed your request."

        pending_booking = ctx.get("pending_booking")
        search_results = ctx.get("last_search")

        return {
            "response": last_ai_message,
            "pending_booking": pending_booking,
            "search_results": search_results,
            "status": "success"
        }
    except Exception as exc:
        logger.error(f"Error running multi-agent system: {exc}", exc_info=True)
        return {
            "response": f"An error occurred while communicating with the Multi-Agent System: {str(exc)}",
            "pending_booking": None,
            "search_results": None,
            "status": "error"
        }
