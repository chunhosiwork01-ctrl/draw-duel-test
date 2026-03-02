#!/usr/bin/env python3
import json
import os
import random
import string
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
INDEX_HTML = ROOT / "index.html"
ASSETS_DIR = ROOT / "assets"
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8765"))
TOTAL_ROUNDS = 3
ROOM_CODE_LEN = 5
MAX_PLAYERS = 6

PROMPTS = [
    {
        "label": "喜羊羊",
        "image": "https://upload.wikimedia.org/wikipedia/en/c/c9/Pleasant_Goat_and_Big_Big_Wolf_characters.jpg",
        "source": "https://en.wikipedia.org/wiki/Pleasant_Goat_and_Big_Big_Wolf",
    },
    {
        "label": "哆啦A夢",
        "image": "https://upload.wikimedia.org/wikipedia/commons/0/02/Takaoka_Doraemon.jpg",
        "source": "https://en.wikipedia.org/wiki/Doraemon",
    },
    {
        "label": "皮卡丘",
        "image": "https://upload.wikimedia.org/wikipedia/en/9/9f/Pikachu_artwork_for_Pok%C3%A9mon_Red_and_Green.png",
        "source": "https://en.wikipedia.org/wiki/Pikachu",
    },
    {
        "label": "湯姆貓",
        "image": "https://upload.wikimedia.org/wikipedia/en/f/f6/TomandJerryTitleCardc.jpg",
        "source": "https://en.wikipedia.org/wiki/Tom_Cat",
    },
]

ROOMS = {}
ROOM_LOCK = threading.Lock()


def json_response(handler, payload, status=HTTPStatus.OK):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def error_response(handler, message, status=HTTPStatus.BAD_REQUEST):
    json_response(handler, {"ok": False, "error": message}, status=status)


def new_room_code():
    while True:
        code = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(ROOM_CODE_LEN))
        if code not in ROOMS:
            return code


def player_summary(room):
    return [
        {
            "id": room["players"][player_id]["id"],
            "name": room["players"][player_id]["name"],
            "is_host": room["players"][player_id]["is_host"],
            "wins": room["players"][player_id]["wins"],
            "total_score": room["players"][player_id]["total_score"],
        }
        for player_id in room["players_order"]
    ]


def fresh_drawing():
    return {"strokes": [], "image": "", "submitted": False}


def fresh_room(name):
    room_code = new_room_code()
    player_id = uuid.uuid4().hex[:8]
    room = {
        "code": room_code,
        "created_at": time.time(),
        "host_id": player_id,
        "players_order": [player_id],
        "players": {
            player_id: {
                "id": player_id,
                "name": name or "Player 1",
                "is_host": True,
                "wins": 0,
                "total_score": 0,
            }
        },
        "stage": "waiting",
        "round_index": -1,
        "prompts": random.sample(PROMPTS, k=min(TOTAL_ROUNDS, len(PROMPTS))),
        "current_prompt": None,
        "current_prompt_image": "",
        "current_prompt_source": "",
        "drawings": {player_id: fresh_drawing()},
        "votes": {},
        "round_result": None,
        "final_winner_id": None,
    }
    ROOMS[room_code] = room
    return room, player_id


def join_room(room_code, name):
    room = ROOMS.get(room_code)
    if not room:
        raise ValueError("Room not found")
    if len(room["players_order"]) >= MAX_PLAYERS:
        raise ValueError("Room is full")

    player_id = uuid.uuid4().hex[:8]
    room["players_order"].append(player_id)
    room["players"][player_id] = {
        "id": player_id,
        "name": name or f"Player {len(room['players_order'])}",
        "is_host": False,
        "wins": 0,
        "total_score": 0,
    }
    room["drawings"][player_id] = fresh_drawing()
    return room, player_id


def start_round(room):
    room["round_index"] += 1
    if room["round_index"] >= TOTAL_ROUNDS:
        room["stage"] = "finished"
        return

    prompt = room["prompts"][room["round_index"]]
    room["current_prompt"] = prompt["label"]
    room["current_prompt_image"] = prompt["image"]
    room["current_prompt_source"] = prompt["source"]
    room["round_result"] = None
    room["votes"] = {}
    room["stage"] = "drawing"
    for player_id in room["players_order"]:
        room["drawings"][player_id] = fresh_drawing()


def all_submitted(room):
    return all(room["drawings"][player_id]["submitted"] for player_id in room["players_order"])


def vote_target_ids(room, voter_id):
    return [player_id for player_id in room["players_order"] if player_id != voter_id]


def all_votes_complete(room):
    if len(room["players_order"]) < 2:
        return False
    for voter_id in room["players_order"]:
        targets = vote_target_ids(room, voter_id)
        cast = room["votes"].get(voter_id, {})
        if any(target_id not in cast for target_id in targets):
            return False
    return True


def vote_totals(room):
    totals = {player_id: {"likes": 0, "eggs": 0} for player_id in room["players_order"]}
    for ballot in room["votes"].values():
        for target_id, choice in ballot.items():
            if target_id not in totals:
                continue
            if choice == "like":
                totals[target_id]["likes"] += 1
            elif choice == "egg":
                totals[target_id]["eggs"] += 1
    return totals


def roast_from_votes(name, prompt, likes, eggs):
    if likes == 0 and eggs == 0:
        return f"{name} 這張目前還沒人敢評，{prompt} 本人可能也在觀望。"
    if likes == 0 and eggs > 0:
        return f"{name} 這張把 {prompt} 畫成了都市傳說，現場雞蛋比掌聲多。"
    if likes > eggs * 2:
        return f"{name} 這張讓大家一眼就認出是 {prompt}，算是本局少數沒被群眾制裁的作品。"
    if likes >= eggs:
        return f"{name} 這張雖然有點走樣，但還保住了 {prompt} 的基本人格。"
    return f"{name} 這張充滿個人風格，只是觀眾一致懷疑 {prompt} 看了會報警。"


def finalize_round(room):
    totals = vote_totals(room)
    result = {
        "scores": {},
        "likes": {},
        "eggs": {},
        "roasts": {},
        "images": {},
        "winner_id": None,
    }

    winner_id = None
    winner_tuple = None
    for player_id in room["players_order"]:
        likes = totals[player_id]["likes"]
        eggs = totals[player_id]["eggs"]
        score = likes * 12 - eggs * 7
        result["scores"][player_id] = score
        result["likes"][player_id] = likes
        result["eggs"][player_id] = eggs
        result["roasts"][player_id] = roast_from_votes(
            room["players"][player_id]["name"],
            room["current_prompt"],
            likes,
            eggs,
        )
        result["images"][player_id] = room["drawings"][player_id].get("image", "")
        ranking = (score, likes, -eggs)
        if winner_tuple is None or ranking > winner_tuple:
            winner_tuple = ranking
            winner_id = player_id

    result["winner_id"] = winner_id
    room["round_result"] = result
    room["stage"] = "results"

    for player_id, score in result["scores"].items():
        room["players"][player_id]["total_score"] += score
    if winner_id:
        room["players"][winner_id]["wins"] += 1

    if room["round_index"] == TOTAL_ROUNDS - 1:
        room["final_winner_id"] = max(
            room["players_order"],
            key=lambda player_id: (
                room["players"][player_id]["wins"],
                room["players"][player_id]["total_score"],
            ),
        )


def sanitize_room(room, player_id):
    drawings = room["drawings"]
    gallery = {}
    if room["stage"] in {"voting", "results", "finished"}:
        for target_id in room["players_order"]:
            gallery[target_id] = drawings[target_id].get("image", "")

    return {
        "ok": True,
        "room_code": room["code"],
        "stage": room["stage"],
        "players": player_summary(room),
        "you": room["players"].get(player_id),
        "prompt": room.get("current_prompt"),
        "prompt_image": room.get("current_prompt_image"),
        "prompt_source": room.get("current_prompt_source"),
        "round_index": room["round_index"],
        "total_rounds": TOTAL_ROUNDS,
        "submitted": drawings.get(player_id, {}).get("submitted", False),
        "submissions": {pid: drawings.get(pid, {}).get("submitted", False) for pid in room["players_order"]},
        "gallery": gallery,
        "votes_cast": room["votes"].get(player_id, {}),
        "vote_target_ids": vote_target_ids(room, player_id) if room["stage"] == "voting" else [],
        "can_start": room["stage"] == "waiting" and room.get("host_id") == player_id and len(room["players_order"]) >= 2,
        "can_next": room["stage"] == "results" and room.get("host_id") == player_id and room["round_index"] < TOTAL_ROUNDS - 1,
        "room_full": len(room["players_order"]) >= MAX_PLAYERS,
        "round_result": room.get("round_result") or {},
        "final_winner_id": room.get("final_winner_id"),
        "max_players": MAX_PLAYERS,
    }


def parse_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    data = handler.rfile.read(length) if length else b"{}"
    try:
        return json.loads(data.decode("utf-8"))
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON body")


class Handler(BaseHTTPRequestHandler):
    server_version = "DrawDuelVotes/0.2"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            raw = INDEX_HTML.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if parsed.path.startswith("/assets/"):
            target = (ROOT / parsed.path.lstrip("/")).resolve()
            if ASSETS_DIR not in target.parents or not target.is_file():
                error_response(self, "Asset not found", HTTPStatus.NOT_FOUND)
                return
            raw = target.read_bytes()
            content_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
            }.get(target.suffix.lower(), "application/octet-stream")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if parsed.path == "/healthz":
            json_response(self, {"ok": True, "status": "healthy"})
            return

        if parsed.path == "/api/state":
            query = parse_qs(parsed.query)
            room_code = (query.get("room") or [""])[0].upper()
            player_id = (query.get("player") or [""])[0]
            with ROOM_LOCK:
                room = ROOMS.get(room_code)
                if not room or player_id not in room["players"]:
                    error_response(self, "Room or player not found", HTTPStatus.NOT_FOUND)
                    return
                json_response(self, sanitize_room(room, player_id))
            return

        error_response(self, "Not found", HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = parse_json(self)
        except ValueError as exc:
            error_response(self, str(exc))
            return

        if parsed.path == "/api/room/create":
            name = str(payload.get("name", "")).strip()[:24]
            with ROOM_LOCK:
                room, player_id = fresh_room(name)
                json_response(self, {"ok": True, "room_code": room["code"], "player_id": player_id})
            return

        if parsed.path == "/api/room/join":
            room_code = str(payload.get("room_code", "")).upper()
            name = str(payload.get("name", "")).strip()[:24]
            with ROOM_LOCK:
                try:
                    room, player_id = join_room(room_code, name)
                except ValueError as exc:
                    error_response(self, str(exc))
                    return
                json_response(self, {"ok": True, "room_code": room["code"], "player_id": player_id})
            return

        if parsed.path in {
            "/api/room/start",
            "/api/room/next",
            "/api/draw/update",
            "/api/draw/submit",
            "/api/vote",
        }:
            room_code = str(payload.get("room_code", "")).upper()
            player_id = str(payload.get("player_id", ""))
            with ROOM_LOCK:
                room = ROOMS.get(room_code)
                if not room or player_id not in room["players"]:
                    error_response(self, "Room or player not found", HTTPStatus.NOT_FOUND)
                    return

                if parsed.path == "/api/room/start":
                    if player_id != room["host_id"]:
                        error_response(self, "Only the host can start")
                        return
                    if len(room["players_order"]) < 2:
                        error_response(self, "Need at least two players to start")
                        return
                    if room["stage"] != "waiting":
                        error_response(self, "Game already started")
                        return
                    start_round(room)
                    json_response(self, sanitize_room(room, player_id))
                    return

                if parsed.path == "/api/room/next":
                    if player_id != room["host_id"]:
                        error_response(self, "Only the host can start next round")
                        return
                    if room["stage"] != "results":
                        error_response(self, "Round is not ready for next step")
                        return
                    if room["round_index"] >= TOTAL_ROUNDS - 1:
                        room["stage"] = "finished"
                    else:
                        start_round(room)
                    json_response(self, sanitize_room(room, player_id))
                    return

                if parsed.path == "/api/draw/update":
                    if room["stage"] != "drawing":
                        error_response(self, "Round is not active")
                        return
                    room["drawings"][player_id]["strokes"] = payload.get("strokes", [])
                    json_response(self, {"ok": True})
                    return

                if parsed.path == "/api/draw/submit":
                    if room["stage"] != "drawing":
                        error_response(self, "Round is not active")
                        return
                    room["drawings"][player_id]["strokes"] = payload.get("strokes", [])
                    room["drawings"][player_id]["image"] = payload.get("image", "")
                    room["drawings"][player_id]["submitted"] = True
                    if all_submitted(room):
                        room["stage"] = "voting"
                        room["votes"] = {}
                    json_response(self, sanitize_room(room, player_id))
                    return

                if parsed.path == "/api/vote":
                    if room["stage"] != "voting":
                        error_response(self, "Voting is not active")
                        return
                    target_id = str(payload.get("target_id", ""))
                    choice = str(payload.get("choice", ""))
                    if target_id == player_id:
                        error_response(self, "You cannot vote for yourself")
                        return
                    if target_id not in room["players"]:
                        error_response(self, "Target not found")
                        return
                    if choice not in {"like", "egg"}:
                        error_response(self, "Invalid vote")
                        return
                    room["votes"].setdefault(player_id, {})[target_id] = choice
                    if all_votes_complete(room):
                        finalize_round(room)
                    json_response(self, sanitize_room(room, player_id))
                    return

        error_response(self, "Not found", HTTPStatus.NOT_FOUND)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Draw duel server running at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
