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
        "slug": "pleasant-goat",
        "image": "/api/reference-image?slug=pleasant-goat",
        "source": "In-game local reference illustration",
    },
    {
        "label": "哆啦A夢",
        "slug": "doraemon",
        "image": "/api/reference-image?slug=doraemon",
        "source": "In-game local reference illustration",
    },
    {
        "label": "皮卡丘",
        "slug": "pikachu",
        "image": "/api/reference-image?slug=pikachu",
        "source": "In-game local reference illustration",
    },
    {
        "label": "湯姆貓",
        "slug": "tom-cat",
        "image": "/api/reference-image?slug=tom-cat",
        "source": "In-game local reference illustration",
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
        "current_prompt_slug": "",
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
    room["current_prompt_slug"] = prompt["slug"]
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
        score = likes - eggs
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
        "prompt_slug": room.get("current_prompt_slug"),
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

        if parsed.path == "/api/reference-image":
            query = parse_qs(parsed.query)
            slug = (query.get("slug") or [""])[0]
            raw = reference_svg(slug).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
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

def reference_svg(slug):
    illustrations = {
        "doraemon": {
            "name": "哆啦A夢",
            "bg": "#dff7ff",
            "body": """
<circle cx="400" cy="420" r="170" fill="#2ca7e0"/>
<circle cx="400" cy="395" r="128" fill="#ffffff"/>
<circle cx="356" cy="316" r="46" fill="#ffffff"/><circle cx="444" cy="316" r="46" fill="#ffffff"/>
<circle cx="370" cy="320" r="12" fill="#111"/><circle cx="430" cy="320" r="12" fill="#111"/>
<circle cx="400" cy="362" r="20" fill="#d83b3b"/>
<rect x="338" y="470" width="124" height="92" rx="48" fill="#ffffff"/>
<path d="M320 380 Q400 455 480 380" stroke="#c02f2f" stroke-width="10" fill="none" stroke-linecap="round"/>
<line x1="290" y1="370" x2="360" y2="360" stroke="#111" stroke-width="6"/>
<line x1="290" y1="394" x2="358" y2="394" stroke="#111" stroke-width="6"/>
<line x1="440" y1="360" x2="510" y2="370" stroke="#111" stroke-width="6"/>
<line x1="442" y1="394" x2="510" y2="394" stroke="#111" stroke-width="6"/>
<rect x="332" y="515" width="136" height="18" rx="9" fill="#f0c23b"/>
""",
        },
        "pikachu": {
            "name": "皮卡丘",
            "bg": "#fff5bf",
            "body": """
<ellipse cx="400" cy="432" rx="150" ry="170" fill="#ffd84a"/>
<path d="M300 214 L262 82 L352 184" fill="#ffd84a"/><path d="M500 214 L542 82 L450 184" fill="#ffd84a"/>
<path d="M278 120 L295 170" stroke="#111" stroke-width="16" stroke-linecap="round"/>
<path d="M522 120 L505 170" stroke="#111" stroke-width="16" stroke-linecap="round"/>
<circle cx="338" cy="372" r="16" fill="#111"/><circle cx="462" cy="372" r="16" fill="#111"/>
<circle cx="286" cy="430" r="26" fill="#e65b5b"/><circle cx="514" cy="430" r="26" fill="#e65b5b"/>
<path d="M366 450 Q400 478 434 450" stroke="#8f5b00" stroke-width="10" fill="none" stroke-linecap="round"/>
<path d="M532 478 L626 420 L590 418 L644 352" stroke="#8f5b00" stroke-width="24" fill="none" stroke-linejoin="round"/>
""",
        },
        "tom-cat": {
            "name": "湯姆貓",
            "bg": "#e5eef7",
            "body": """
<ellipse cx="400" cy="434" rx="156" ry="176" fill="#7e8793"/>
<path d="M290 270 L242 134 L352 218" fill="#7e8793"/><path d="M510 270 L558 134 L448 218" fill="#7e8793"/>
<path d="M300 244 L270 178 L332 224" fill="#f1b4c8"/><path d="M500 244 L530 178 L468 224" fill="#f1b4c8"/>
<ellipse cx="400" cy="440" rx="118" ry="130" fill="#d9dde2"/>
<circle cx="350" cy="372" r="16" fill="#c5e55a"/><circle cx="450" cy="372" r="16" fill="#c5e55a"/>
<circle cx="350" cy="372" r="8" fill="#111"/><circle cx="450" cy="372" r="8" fill="#111"/>
<path d="M344 452 Q400 490 456 452" stroke="#111" stroke-width="10" fill="none" stroke-linecap="round"/>
<path d="M330 418 L260 394 M330 432 L250 432 M330 446 L260 470" stroke="#111" stroke-width="6" stroke-linecap="round"/>
<path d="M470 418 L540 394 M470 432 L550 432 M470 446 L540 470" stroke="#111" stroke-width="6" stroke-linecap="round"/>
<path d="M190 430 Q150 340 194 264" stroke="#7e8793" stroke-width="18" fill="none" stroke-linecap="round"/>
""",
        },
        "pleasant-goat": {
            "name": "喜羊羊",
            "bg": "#f2f8ff",
            "body": """
<circle cx="400" cy="420" r="154" fill="#ffffff"/>
<circle cx="400" cy="314" r="126" fill="#fffdf7"/>
<path d="M314 228 Q260 160 298 112" stroke="#b78bd6" stroke-width="18" fill="none" stroke-linecap="round"/>
<path d="M486 228 Q540 160 502 112" stroke="#b78bd6" stroke-width="18" fill="none" stroke-linecap="round"/>
<ellipse cx="322" cy="318" rx="20" ry="28" fill="#b78bd6"/><ellipse cx="478" cy="318" rx="20" ry="28" fill="#b78bd6"/>
<circle cx="356" cy="314" r="16" fill="#111"/><circle cx="444" cy="314" r="16" fill="#111"/>
<path d="M362 374 Q400 398 438 374" stroke="#111" stroke-width="10" fill="none" stroke-linecap="round"/>
<circle cx="400" cy="350" r="12" fill="#d6924a"/>
<path d="M342 220 Q400 168 458 220" stroke="#ffffff" stroke-width="22" fill="none" stroke-linecap="round"/>
<path d="M348 224 Q400 184 452 224" stroke="#dfe8f0" stroke-width="10" fill="none" stroke-linecap="round"/>
<rect x="334" y="492" width="132" height="28" rx="14" fill="#4d83d3"/>
<circle cx="400" cy="506" r="16" fill="#f0d45d"/>
""",
        },
    }
    item = illustrations.get(slug, {
        "name": "角色參考圖",
        "bg": "#1b2432",
        "body": '<rect x="220" y="220" width="360" height="360" rx="40" fill="#273447"/>',
    })
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800">
<rect width="800" height="800" fill="{item['bg']}"/>
<rect x="34" y="34" width="732" height="732" rx="36" fill="rgba(255,255,255,0.28)" stroke="#f18f5c" stroke-width="8"/>
<text x="400" y="110" text-anchor="middle" font-size="56" fill="#1a2230" font-family="Arial">角色參考圖</text>
{item['body']}
<text x="400" y="710" text-anchor="middle" font-size="74" fill="#1a2230" font-family="Arial">{item['name']}</text>
</svg>"""


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Draw duel server running at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
