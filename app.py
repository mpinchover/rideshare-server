"""Local rideshare match server.

Pairs two open sockets at the same airport when their destinations are
within 5 miles, choosing the closest pair.

    python -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python app.py
"""

import csv
import logging
import math
import os
import re
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit

from chat import register_chat

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5050"))
AIRPORT_CODE = re.compile(r"^[A-Z]{3}$")
USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SEARCH_MILES = 50
MATCH_MILES = 5
EARTH_MILES = 3958.7613
HERE = Path(__file__).resolve().parent
LOCAL_WEB_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
]
WEB_ORIGINS = [
    *LOCAL_WEB_ORIGINS,
    *[
        origin.strip().rstrip("/")
        for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ],
]

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("rideshare")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "rideshare-local")
socketio = SocketIO(
    app,
    cors_allowed_origins=WEB_ORIGINS,
    async_mode="threading",
)


def miles_between(lat1, lon1, lat2, lon2):
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_MILES * math.asin(math.sqrt(a))


def destination_key(city, state):
    return f"{city.casefold()}|{state.upper()}"


def _load_places():
    with (HERE / "us_cities.csv").open(newline="") as handle:
        cities = []
        for row in csv.DictReader(handle):
            city = " ".join(row["city"].split())
            state = row["state"].strip().upper()
            if not city or not state:
                continue
            cities.append(
                {
                    "city": city,
                    "state": state,
                    "latitude": float(row["latitude"]),
                    "longitude": float(row["longitude"]),
                }
            )

    airports = {}
    destinations = {}
    with (HERE / "top_us_airports.csv").open(newline="") as handle:
        for row in csv.DictReader(handle):
            code = row["iata_code"].strip().upper()
            latitude = float(row["latitude"])
            longitude = float(row["longitude"])
            airports[code] = {
                "code": code,
                "name": " ".join(row["name"].split()),
                "latitude": latitude,
                "longitude": longitude,
            }
            closest = {}
            for city in cities:
                distance = miles_between(
                    latitude,
                    longitude,
                    city["latitude"],
                    city["longitude"],
                )
                if distance > SEARCH_MILES:
                    continue
                key = destination_key(city["city"], city["state"])
                current = closest.get(key)
                if current is not None and current["miles"] <= distance:
                    continue
                closest[key] = {
                    "city": city["city"],
                    "state": city["state"],
                    "miles": round(distance, 2),
                    "latitude": city["latitude"],
                    "longitude": city["longitude"],
                }
            destinations[code] = sorted(
                closest.values(),
                key=lambda item: (item["miles"], item["city"], item["state"]),
            )
    return airports, destinations


AIRPORTS, DESTINATIONS = _load_places()


def selectable(airport, city, state):
    wanted = destination_key(city, state)
    for destination in DESTINATIONS.get(airport, []):
        if destination_key(destination["city"], destination["state"]) != wanted:
            continue
        return destination
    return None


class Lobby:
    """Unmatched riders at an airport. Destinations within 5 miles can match."""

    def __init__(self):
        self.lock = threading.Lock()
        self.waiting = {}
        self.riders = {}

    def join(self, sid, name, airport, city, state, user_id, latitude, longitude, is_open):
        existing = self.riders.get(sid)
        if existing:
            if existing["status"] == "waiting":
                return {"kind": "error", "message": "You're already waiting for a match."}
            # A closed pair stays in its chat. Looking again starts a new one.
            self.riders.pop(sid, None)

        key = airport
        queue = self.waiting.setdefault(key, [])
        same_rider = []
        too_far = []
        candidates = []
        while queue:
            candidate = queue.pop(0)
            partner = self.riders.get(candidate)
            if partner is None or partner["status"] != "waiting" or candidate == sid:
                continue
            if not is_open(candidate):
                self.riders.pop(candidate, None)
                continue
            distance = miles_between(
                latitude,
                longitude,
                partner["latitude"],
                partner["longitude"],
            )
            if partner["user_id"] == user_id:
                if distance <= MATCH_MILES:
                    same_rider.append(candidate)
                else:
                    too_far.append(candidate)
                continue
            if distance > MATCH_MILES:
                too_far.append(candidate)
                continue
            candidates.append((distance, candidate))

        partner_sid = None
        partner_distance = None
        if candidates:
            candidates.sort(key=lambda item: item[0])
            partner_distance, partner_sid = candidates[0]
            queue[0:0] = too_far + [item for _, item in candidates[1:]] + same_rider
        else:
            queue[0:0] = too_far + same_rider
            if same_rider:
                return {
                    "kind": "error",
                    "message": "Both pages are signed in as the same rider. Open the other person in a private window.",
                }

        if partner_sid is None:
            self.riders[sid] = {
                "name": name,
                "airport": airport,
                "city": city,
                "state": state,
                "latitude": latitude,
                "longitude": longitude,
                "user_id": user_id,
                "key": key,
                "status": "waiting",
                "partner_sid": None,
            }
            queue.append(sid)
            return {
                "kind": "waiting",
                "payload": {
                    "name": name,
                    "airport": airport,
                    "city": city,
                    "state": state,
                    "userId": user_id,
                },
            }

        partner = self.riders[partner_sid]
        channel_id = "m" + uuid.uuid4().hex
        self.riders[sid] = {
            "name": name,
            "airport": airport,
            "city": city,
            "state": state,
            "latitude": latitude,
            "longitude": longitude,
            "user_id": user_id,
            "key": key,
            "status": "matched",
            "partner_sid": partner_sid,
        }
        partner["status"] = "matched"
        partner["partner_sid"] = sid
        if not queue:
            self.waiting.pop(key, None)

        you = {
            "name": name,
            "airport": airport,
            "city": city,
            "state": state,
            "userId": user_id,
        }
        them = {
            "name": partner["name"],
            "airport": partner["airport"],
            "city": partner["city"],
            "state": partner["state"],
            "userId": partner["user_id"],
        }
        return {
            "kind": "matched",
            "partner_sid": partner_sid,
            "distance": round(partner_distance, 2),
            "for_you": {"channelId": channel_id, "you": you, "partner": them},
            "for_partner": {"channelId": channel_id, "you": them, "partner": you},
        }

    def leave(self, sid):
        rider = self.riders.pop(sid, None)
        if rider is None:
            return None

        if rider["status"] != "waiting":
            # A matched rider's chat lives on Stream. Closing the socket is not leaving the chat.
            return None

        queue = self.waiting.get(rider["key"], [])
        self.waiting[rider["key"]] = [item for item in queue if item != sid]
        if not self.waiting[rider["key"]]:
            self.waiting.pop(rider["key"], None)
        return None


lobby = Lobby()


def _clean_lookup(data):
    if not isinstance(data, dict) or not isinstance(data.get("airport"), str):
        return None, "Use a 3-letter airport code."
    airport = data["airport"].strip().upper()
    if AIRPORT_CODE.fullmatch(airport) is None:
        return None, "Use a 3-letter airport code."
    if airport not in AIRPORTS:
        return None, "That airport isn't in the list."
    return airport, None


def _coordinate(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if math.isnan(number) or math.isinf(number) or number < low or number > high:
        return None
    return number


def _clean_join(data):
    if not isinstance(data, dict):
        return None, "Name, airport, and city are required."
    name = data.get("name")
    airport = data.get("airport")
    city = data.get("city")
    state = data.get("state")
    user_id = data.get("userId")
    latitude = _coordinate(data.get("latitude"), -90, 90)
    longitude = _coordinate(data.get("longitude"), -180, 180)
    if not all(isinstance(value, str) for value in (name, airport, city, state, user_id)):
        return None, "Name, airport, and city are required."
    name = " ".join(name.split())
    airport = airport.strip().upper()
    city = " ".join(city.split())
    state = state.strip().upper()
    if not name or len(name) > 40:
        return None, "Enter a name up to 40 characters."
    if USER_ID.fullmatch(user_id) is None:
        return None, "That rider id isn't valid."
    if AIRPORT_CODE.fullmatch(airport) is None or airport not in AIRPORTS:
        return None, "Use an airport from the list."
    if not city or len(city) > 100 or re.fullmatch(r"[A-Z]{2}", state) is None:
        return None, "Choose a neighborhood."
    if latitude is None or longitude is None:
        chosen = selectable(airport, city, state)
        if chosen is None:
            return None, "Choose a neighborhood."
        latitude = chosen["latitude"]
        longitude = chosen["longitude"]
        city = chosen["city"]
        state = chosen["state"]
    return (name, airport, city, state, user_id, latitude, longitude), None


def _is_open(sid):
    return socketio.server.manager.is_connected(sid, "/")


def _send(outcome):
    if outcome["kind"] == "error":
        emit("join_error", {"message": outcome["message"]})
        return
    if outcome["kind"] == "waiting":
        person = outcome["payload"]
        log.info("waiting %s for %s, %s", person["name"], person["city"], person["state"])
        emit("waiting", person)
        return
    partner = outcome["for_you"]["partner"]
    log.info(
        "matched %s (%s) with %s (%s), %.2f mi apart",
        outcome["for_you"]["you"]["name"],
        outcome["for_you"]["you"]["city"],
        partner["name"],
        partner["city"],
        outcome["distance"],
    )
    emit("matched", outcome["for_you"])
    socketio.emit("matched", outcome["for_partner"], to=outcome["partner_sid"])


def _for_web(body, status=200):
    response = jsonify(body)
    response.status_code = status
    origin = request.headers.get("Origin")
    if origin in WEB_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return response


register_chat(app, _for_web, HERE)


@app.get("/health")
def health():
    return _for_web({"ok": True})


@app.get("/destinations/<airport_code>")
def destinations(airport_code):
    airport, error = _clean_lookup({"airport": airport_code})
    if error:
        return _for_web({"error": error}, 400)
    place = AIRPORTS[airport]
    return _for_web(
        {
            "airport": airport,
            "airportName": place["name"],
            "destinations": [
                {"city": item["city"], "state": item["state"], "miles": item["miles"]}
                for item in DESTINATIONS[airport]
            ],
        }
    )


@socketio.on("lookup")
def handle_lookup(data):
    airport, error = _clean_lookup(data)
    if error:
        emit("lookup_error", {"message": error})
        return
    place = AIRPORTS[airport]
    emit(
        "destinations",
        {
            "airport": airport,
            "airportName": place["name"],
            "destinations": [
                {"city": item["city"], "state": item["state"], "miles": item["miles"]}
                for item in DESTINATIONS[airport]
            ],
        },
    )


@socketio.on("join")
def handle_join(data):
    cleaned, error = _clean_join(data)
    if error:
        emit("join_error", {"message": error})
        return
    name, airport, city, state, user_id, latitude, longitude = cleaned
    with lobby.lock:
        outcome = lobby.join(
            request.sid,
            name,
            airport,
            city,
            state,
            user_id,
            latitude,
            longitude,
            _is_open,
        )
    _send(outcome)


@socketio.on("connect")
def handle_connect():
    log.info("socket open %s", request.sid)


@socketio.on("disconnect")
def handle_disconnect(reason=None):
    log.info("socket closed %s", request.sid)
    with lobby.lock:
        lobby.leave(request.sid)


if __name__ == "__main__":
    log.info(
        "Rideshare server on http://%s:%s (%s airports)",
        HOST,
        PORT,
        len(AIRPORTS),
    )
    socketio.run(app, host=HOST, port=PORT, allow_unsafe_werkzeug=True)
