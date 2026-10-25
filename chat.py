"""Stream Chat operations. The API secret stays in this process."""

import os
import re

from flask import request
from stream_chat import StreamChat
from stream_chat.base.exceptions import StreamAPIException

PUBLIC_STREAM_API_KEY = "dftap49bg6c2"
USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
CHANNEL_ID = re.compile(r"^m[a-f0-9]{32}$")
AIRPORT_CODE = re.compile(r"^[A-Z]{3}$")
SAME_RIDER = (
    "Both pages are signed in as the same rider. Open the other person in a private window."
)


def _load_local_env(path, override=False):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if override or key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _api_key():
    return (
        os.environ.get("NEXT_PUBLIC_STREAM_API_KEY", "").strip()
        or os.environ.get("STREAM_API_KEY", "").strip()
        or PUBLIC_STREAM_API_KEY
    )


def _client():
    secret = os.environ.get("STREAM_API_SECRET", "").strip()
    if not secret:
        return None
    return StreamChat(api_key=_api_key(), api_secret=secret)


def _members(body):
    if not isinstance(body, dict) or not isinstance(body.get("members"), list):
        return None, "A match chat needs two riders."
    if len(body["members"]) != 2:
        return None, "A match chat needs two riders."
    cleaned = []
    for member in body["members"]:
        if not isinstance(member, dict):
            return None, "A match chat needs two riders."
        user_id = member.get("id")
        name = member.get("name")
        city = member.get("city")
        state = member.get("state")
        if not all(isinstance(value, str) for value in (user_id, name, city, state)):
            return None, "A match chat needs two riders."
        name = " ".join(name.split())
        city = " ".join(city.split())
        state = state.strip().upper()
        if (
            USER_ID.fullmatch(user_id) is None
            or not name
            or len(name) > 40
            or len(city) > 100
            or (state and re.fullmatch(r"[A-Z]{2}", state) is None)
        ):
            return None, "A match chat needs two riders."
        cleaned.append({"id": user_id, "name": name, "city": city, "state": state})
    if cleaned[0]["id"] == cleaned[1]["id"]:
        return None, SAME_RIDER
    return cleaned, None


def _channel_payload(channel, user_id):
    data = channel.get("channel") or channel
    members = channel.get("members") or data.get("members") or []
    partner = next(
        (
            member
            for member in members
            if (member.get("user_id") or (member.get("user") or {}).get("id")) != user_id
        ),
        None,
    )
    if partner is None:
        return None
    partner_id = partner.get("user_id") or (partner.get("user") or {}).get("id")
    partner_name = ((partner.get("user") or {}).get("name") or "").strip()
    if not isinstance(partner_id, str) or USER_ID.fullmatch(partner_id) is None:
        return None
    destinations = data.get("destinations") or {}
    states = data.get("destination_states") or data.get("destinationStates") or {}
    return {
        "channelId": data.get("id"),
        "airport": data.get("airport") or "",
        "city": destinations.get(user_id) or "",
        "state": states.get(user_id) or "",
        "partnerId": partner_id,
        "partnerName": partner_name or "Rider",
        "partnerCity": destinations.get(partner_id) or "",
        "partnerState": states.get(partner_id) or "",
    }


def register_chat(app, respond, here):
    _load_local_env(here / ".env")
    _load_local_env(here / ".env.local", override=True)

    @app.route("/chat/token", methods=["OPTIONS"])
    @app.route("/chat/channel", methods=["OPTIONS"])
    def chat_options():
        return respond({})

    @app.get("/chat/token")
    def chat_token():
        user_id = request.args.get("user_id", "")
        name = " ".join(request.args.get("name", "").split())
        if USER_ID.fullmatch(user_id) is None or not name or len(name) > 40:
            return respond({"error": "A name and rider id are required."}, 400)
        client = _client()
        if client is None:
            return respond({"error": "Chat is not configured."}, 500)
        client.upsert_users([{"id": user_id, "name": name, "role": "user"}])
        try:
            result = client.query_channels(
                {"type": "messaging", "members": {"$in": [user_id]}},
                limit=30,
                state=False,
                watch=False,
            )
            for item in result.get("channels") or []:
                data = item.get("channel") or {}
                if isinstance(data.get("name"), str) and data["name"] and data.get("id"):
                    client.channel("messaging", data["id"]).update_partial(to_unset=["name"])
        except StreamAPIException as cause:
            app.logger.error("chat name clear failed %s", cause)
        return respond({"chatToken": client.create_token(user_id)})

    @app.get("/chat/channel")
    def chat_lookup():
        user_id = request.args.get("user_id", "")
        if USER_ID.fullmatch(user_id) is None:
            return respond({"error": "A rider id is required."}, 400)
        client = _client()
        if client is None:
            return respond({"error": "Chat is not configured."}, 500)
        try:
            result = client.query_channels(
                {"type": "messaging", "members": {"$in": [user_id]}},
                sort=[{"last_updated": -1}],
                limit=30,
                state=True,
                watch=False,
                message_limit=0,
                member_limit=10,
                user_id=user_id,
            )
        except StreamAPIException as cause:
            app.logger.error("chat lookup failed %s", cause)
            return respond({"error": "Couldn't look up the chat."}, 502)
        for item in result.get("channels") or []:
            data = item.get("channel") or {}
            if CHANNEL_ID.fullmatch(data.get("id") or "") is None:
                continue
            payload = _channel_payload(item, user_id)
            if payload and payload["channelId"]:
                return respond({"channel": payload})
        return respond({"channel": None})

    @app.post("/chat/channel")
    def chat_open():
        body = request.get_json(silent=True)
        members, error = _members(body)
        channel_id = body.get("channelId") if isinstance(body, dict) else None
        created_by = body.get("createdBy") if isinstance(body, dict) else None
        airport = body.get("airport") if isinstance(body, dict) else None
        if (
            error
            or not isinstance(channel_id, str)
            or CHANNEL_ID.fullmatch(channel_id) is None
            or not isinstance(created_by, str)
            or not any(member["id"] == created_by for member in members or [])
            or not isinstance(airport, str)
            or AIRPORT_CODE.fullmatch(airport) is None
        ):
            return respond({"error": error or "A match chat needs two riders."}, 400)
        client = _client()
        if client is None:
            return respond({"error": "Chat is not configured."}, 500)
        destinations = {member["id"]: member["city"] for member in members}
        states = {member["id"]: member["state"] for member in members}
        try:
            client.upsert_users(
                [
                    {"id": member["id"], "name": member["name"], "role": "user"}
                    for member in members
                ]
            )
            channel = client.channel(
                "messaging",
                channel_id,
                {
                    "members": [member["id"] for member in members],
                    "airport": airport,
                    "destinations": destinations,
                    "destinationStates": states,
                },
            )
            channel.create(created_by)
            channel.update_partial(
                to_set={
                    "airport": airport,
                    "destinations": destinations,
                    "destinationStates": states,
                },
                to_unset=["name"],
            )
        except StreamAPIException as cause:
            message = str(cause)
            if "already exists" in message.lower():
                client.channel("messaging", channel_id).update_partial(to_unset=["name"])
                return respond({"channelId": channel_id, "type": "messaging"})
            app.logger.error("chat channel failed %s", message)
            return respond({"error": "Couldn't open the chat."}, 502)
        return respond({"channelId": channel_id, "type": "messaging"})

    @app.delete("/chat/channel")
    def chat_delete():
        body = request.get_json(silent=True)
        channel_id = body.get("channelId") if isinstance(body, dict) else None
        user_id = body.get("userId") if isinstance(body, dict) else None
        if (
            not isinstance(channel_id, str)
            or CHANNEL_ID.fullmatch(channel_id) is None
            or not isinstance(user_id, str)
            or USER_ID.fullmatch(user_id) is None
        ):
            return respond({"error": "That chat could not be ended."}, 400)
        client = _client()
        if client is None:
            return respond({"error": "Chat is not configured."}, 500)
        channel = client.channel("messaging", channel_id)
        try:
            state = channel.query(state=True, watch=False, messages={"limit": 0})
            members = state.get("members") or []
            member_ids = [
                member.get("user_id") or (member.get("user") or {}).get("id")
                for member in members
            ]
            if user_id not in member_ids:
                return respond({"error": "That chat isn't yours."}, 403)
            channel.delete(hard=True)
        except StreamAPIException as cause:
            message = str(cause)
            if re.search(r"not found|doesn't exist|does not exist", message, re.I):
                return respond({"ok": True})
            app.logger.error("chat channel delete failed %s", message)
            return respond({"error": "Couldn't end the chat."}, 502)
        return respond({"ok": True})
