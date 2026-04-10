# src/game/api.py
import time
import uuid
from fastapi import HTTPException

from .constants import MIN_PLAYERS, MAX_PLAYERS, PHASE_LOBBY
from .util import norm_room, generate_username
from .state import get_room, rooms, room_last_activity, room_public_snapshot, Player
from .engine import enter_chat_phase

def register_api(app, *, get_sink, broadcast, get_shadow_ai):
    @app.get("/api/rooms")
    async def list_rooms():
        now = time.time()
        out = []
        for rid, room in rooms.items():
            out.append({
                "id": rid,
                "users": len(room.players),
                "lastActivity": int(now - room_last_activity[rid]),
                "phase": room.phase,
                "round": room.round,
            })
        out.sort(key=lambda r: r["lastActivity"])
        return out

    @app.post("/api/rooms")
    async def create_room(payload: dict):
        rid = norm_room(payload.get("id"))
        if not rid:
            raise HTTPException(400, "Missing id")
        get_room(rid)
        room_last_activity[rid] = time.time()
        return {"id": rid}

    @app.post("/api/rooms/{room_id}/join")
    async def join_room(room_id: str, payload: dict):
        rid = norm_room(room_id)
        room = get_room(rid)

        if len(room.players) >= MAX_PLAYERS:
            raise HTTPException(400, f"Room full (max {MAX_PLAYERS}).")

        display_name = (payload.get("displayName") or "").strip()[:64]
        if not display_name:
            raise HTTPException(400, "Display name is required.")

        # Prevent the same display name from joining the same room twice
        for p in room.players.values():
            if not p.is_ai and p.display_name.lower() == display_name.lower():
                raise HTTPException(400, "A player with that name has already joined this room.")

        taken = {p.username for p in room.players.values()}
        username = generate_username(taken)
        player_id = str(uuid.uuid4())
        participant_id = (payload.get("participantId") or "").strip()[:64]
        age = max(0, int(payload.get("age") or 0))

        is_first = (len(room.players) == 0)
        room.players[player_id] = Player(
            player_id=player_id,
            username=username,
            display_name=display_name,
            participant_id=participant_id,
            age=age,
        )

        if is_first:
            room.host_player_id = player_id
            room.phase = PHASE_LOBBY
            room.round = 0

        joined_at = int(time.time())
        room_last_activity[room.room_id] = joined_at

        sink = get_sink()
        sink.emit_player(player_id, rid, username, display_name, participant_id, age, joined_at)

        return {
            "roomId": room.room_id,
            "playerId": player_id,
            "username": username,
            "displayName": display_name,
            "isHost": (player_id == room.host_player_id),
            "snapshot": room_public_snapshot(room),
        }

    @app.post("/api/rooms/{room_id}/start")
    async def start_game(room_id: str, payload: dict):
        rid = norm_room(room_id)
        room = get_room(rid)

        caller = (payload.get("playerId") or "").strip()
        if not caller:
            raise HTTPException(400, "Missing playerId")

        if caller != room.host_player_id:
            raise HTTPException(403, "Only host can start the game.")

        n = len(room.players)
        if n < MIN_PLAYERS:
            raise HTTPException(400, f"Need at least {MIN_PLAYERS} players to start.")
        if n > MAX_PLAYERS:
            raise HTTPException(400, f"Too many players (max {MAX_PLAYERS}).")

        if room.phase != PHASE_LOBBY:
            raise HTTPException(400, "Game already started.")

        # reset game
        room.round = 1
        room.votes_by_round.clear()
        room.phase_task_id += 1
        room.chat_ends_at = None
        room.vote_ends_at = None

        # remove all previous AI players from the room (if any)
        for pid in [pid for pid, p in room.players.items() if p.is_ai]:
            del room.players[pid]
        room.ai_player_id = None

        # reset all remaining (human) players
        for p in room.players.values():
            p.eliminated = False
            p.is_ai = False

        # add one AI player per human — equal numbers so neither side has an advantage
        human_pids = list(room.players.keys())  # capture before adding AIs
        taken = {p.username for p in room.players.values()}
        ai_pids = []
        for _ in range(len(human_pids)):
            ai_username = generate_username(taken)
            taken.add(ai_username)
            ai_pid = str(uuid.uuid4())
            room.players[ai_pid] = Player(
                player_id=ai_pid,
                username=ai_username,
                is_ai=True,
            )
            ai_pids.append(ai_pid)

        room_last_activity[room.room_id] = time.time()

        shadow_ai = get_shadow_ai()
        shadow_ai.reset_for_room(dict(zip(ai_pids, human_pids)))

        await enter_chat_phase(room, broadcast)
        return {"ok": True, "snapshot": room_public_snapshot(room)}

    @app.get("/api/rooms/{room_id}/history")
    async def room_history(room_id: str, limit: int = 50):
        rid = norm_room(room_id)
        sink = get_sink()
        msgs = sink.recent_messages(rid, limit=limit)
        return {"roomId": rid, "messages": msgs}
