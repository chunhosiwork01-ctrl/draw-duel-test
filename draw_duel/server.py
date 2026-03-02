#!/usr/bin/env python3
import base64
import copy
import hashlib
import json
import os
import random
import string
import threading
import time
import urllib.error
import urllib.request
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

PROMPTS = [
    {"label": "喜羊羊", "image": "/assets/xiyangyang.png"},
    {"label": "哆啦A夢", "image": "/assets/doraemon.png"},
    {"label": "海綿寶寶", "image": "/assets/spongebob.png"},
    {"label": "皮卡丘", "image": "/assets/pikachu.png"},
    {"label": "小丸子", "image": "/assets/chibi-maruko.png"},
    {"label": "蠟筆小新", "image": "/assets/shinchan.png"},
    {"label": "米老鼠", "image": "/assets/mickey-mouse.png"},
    {"label": "湯姆貓", "image": "/assets/tom-cat.png"},
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
    ordered = []
    for player_id in room["players_order"]:
        player = room["players"][player_id]
        ordered.append({
            "id": player["id"],
            "name": player["name"],
            "is_host": player["is_host"],
            "wins": player["wins"],
            "total_score": player["total_score"],
        })
    return ordered


def sanitize_room(room, player_id):
    drawings = room["drawings"]
    result = room.get("round_result") or {}
    return {
        "ok": True,
        "room_code": room["code"],
        "stage": room["stage"],
        "players": player_summary(room),
        "you": room["players"].get(player_id),
        "prompt": room.get("current_prompt"),
        "prompt_image": room.get("current_prompt_image"),
        "round_index": room["round_index"],
        "total_rounds": TOTAL_ROUNDS,
        "submitted": drawings.get(player_id, {}).get("submitted", False),
        "submissions": {pid: drawings.get(pid, {}).get("submitted", False) for pid in room["players_order"]},
        "judging_started_at": room.get("judging_started_at"),
        "can_start": room["stage"] == "waiting" and room.get("host_id") == player_id and len(room["players_order"]) == 2,
        "can_next": room["stage"] == "results" and room.get("host_id") == player_id and room["round_index"] < TOTAL_ROUNDS - 1,
        "room_full": len(room["players_order"]) == 2,
        "round_result": result,
        "final_winner_id": room.get("final_winner_id"),
    }


def fresh_drawing():
    return {"strokes": [], "image": "", "submitted": False}


def create_room(name):
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
        "drawings": {player_id: fresh_drawing()},
        "round_result": None,
        "final_winner_id": None,
        "judging_started_at": None,
    }
    ROOMS[room_code] = room
    return room, player_id


def join_room(room_code, name):
    room = ROOMS.get(room_code)
    if not room:
        raise ValueError("Room not found")
    if len(room["players_order"]) >= 2:
        raise ValueError("Room is full")
    player_id = uuid.uuid4().hex[:8]
    room["players_order"].append(player_id)
    room["players"][player_id] = {
        "id": player_id,
        "name": name or "Player 2",
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
    room["current_prompt"] = room["prompts"][room["round_index"]]
    room["current_prompt_image"] = room["current_prompt"].get("image", "")
    room["current_prompt"] = room["current_prompt"]["label"]
    room["stage"] = "drawing"
    room["round_result"] = None
    room["judging_started_at"] = None
    for player_id in room["players_order"]:
        room["drawings"][player_id] = fresh_drawing()


def collect_fallback_metrics(strokes):
    points = 0
    colors = set()
    min_x = 10000
    min_y = 10000
    max_x = -1
    max_y = -1

    for stroke in strokes:
        colors.add(stroke.get("color", "#000000"))
        pts = stroke.get("points", [])
        points += len(pts)
        for point in pts:
            x = point.get("x", 0)
            y = point.get("y", 0)
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)

    width = 0 if max_x < min_x else max_x - min_x
    height = 0 if max_y < min_y else max_y - min_y
    area = width * height
    return {
        "points": points,
        "color_count": len(colors),
        "area": area,
    }


def fallback_judge(room):
    prompt = room["current_prompt"]
    result = {"scores": {}, "roasts": {}, "winner_id": None, "images": {}}
    winner_score = -1
    winner_id = None

    for player_id in room["players_order"]:
        drawing = room["drawings"][player_id]
        metrics = collect_fallback_metrics(drawing["strokes"])
        signature = hashlib.sha256((prompt + json.dumps(drawing["strokes"], ensure_ascii=False)).encode("utf-8")).hexdigest()
        bonus = int(signature[:2], 16) % 18
        complexity = min(metrics["points"] // 8, 35)
        coverage = min(metrics["area"] // 3500, 25)
        color_score = min(metrics["color_count"] * 5, 15)
        score = max(8, min(100, 20 + complexity + coverage + color_score + bonus))
        roast = fallback_roast(prompt, room["players"][player_id]["name"], score, metrics)
        result["scores"][player_id] = score
        result["roasts"][player_id] = roast
        result["images"][player_id] = drawing.get("image", "")
        if score > winner_score:
            winner_score = score
            winner_id = player_id

    result["winner_id"] = winner_id
    return result


def fallback_roast(prompt, name, score, metrics):
    if metrics["points"] < 10:
        return f"{name}，你這張像是剛打開畫板就投降。{prompt} 還沒出場，靈魂先下線。"
    if score < 35:
        return f"{name} 畫的這位，像 {prompt} 的遠房表弟，走失很多年都沒找回來。"
    if score < 60:
        return f"{name} 至少有抓到一點記憶，但這版本的 {prompt} 比較像凌晨三點趕稿的同人分身。"
    if score < 80:
        return f"{name} 這張已經看得出是 {prompt}，只是還帶著一股『今天精神不太穩定』的氣質。"
    return f"{name} 這張居然真有幾分神韻。雖然還不是官方原稿，但至少不會被叫成怪物。"


def _extract_output_text(payload):
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"]
    fragments = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                fragments.append(text)
    return "\n".join(fragments)


def openai_judge(room):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    player_ids = room["players_order"]
    images = [room["drawings"][pid].get("image", "") for pid in player_ids]
    if not all(images):
        raise RuntimeError("Missing submitted image")

    prompt = room["current_prompt"]
    player_names = [room["players"][pid]["name"] for pid in player_ids]
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "You are a strict but funny drawing contest judge. "
                            "Return JSON only with keys scores, roasts, winner_index. "
                            "scores must be an array of two integers 0-100. "
                            "roasts must be an array of two short playful roast strings. "
                            "winner_index must be 0 or 1."
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            f"Target character: {prompt}. "
                            f"Player A is {player_names[0]}. Player B is {player_names[1]}. "
                            "Judge visual similarity from memory drawing quality, recognizability, and shape resemblance."
                        ),
                    },
                    {"type": "input_image", "image_url": images[0]},
                    {"type": "input_image", "image_url": images[1]},
                ],
            },
        ],
    }

    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        raw = json.loads(response.read().decode("utf-8"))

    text = _extract_output_text(raw).strip()
    parsed = json.loads(text)
    scores = parsed["scores"]
    roasts = parsed["roasts"]
    winner_index = int(parsed["winner_index"])
    result = {
        "scores": {
            player_ids[0]: int(scores[0]),
            player_ids[1]: int(scores[1]),
        },
        "roasts": {
            player_ids[0]: str(roasts[0]),
            player_ids[1]: str(roasts[1]),
        },
        "winner_id": player_ids[winner_index],
        "images": {
            player_ids[0]: images[0],
            player_ids[1]: images[1],
        },
        "judge_mode": "openai",
    }
    return result


def judge_round(room):
    try:
        result = openai_judge(room)
    except Exception as exc:
        result = fallback_judge(room)
        result["judge_mode"] = "fallback"
        result["judge_error"] = str(exc)

    room["round_result"] = result
    room["stage"] = "results"
    room["judging_started_at"] = None

    for player_id, score in result["scores"].items():
        room["players"][player_id]["total_score"] += score
    if result["winner_id"]:
        room["players"][result["winner_id"]]["wins"] += 1

    if room["round_index"] == TOTAL_ROUNDS - 1:
        room["final_winner_id"] = max(
            room["players_order"],
            key=lambda pid: (
                room["players"][pid]["wins"],
                room["players"][pid]["total_score"],
            ),
        )


def begin_judging(room_code):
    with ROOM_LOCK:
        room = ROOMS.get(room_code)
        if not room or room["stage"] != "judging":
            return
        room_snapshot = copy.deepcopy(room)

    try:
        result = openai_judge(room_snapshot)
    except Exception as exc:
        result = fallback_judge(room_snapshot)
        result["judge_mode"] = "fallback"
        result["judge_error"] = str(exc)

    with ROOM_LOCK:
        room = ROOMS.get(room_code)
        if not room or room["stage"] != "judging":
            return

        room["round_result"] = result
        room["stage"] = "results"
        room["judging_started_at"] = None

        for player_id, score in result["scores"].items():
            room["players"][player_id]["total_score"] += score
        if result["winner_id"]:
            room["players"][result["winner_id"]]["wins"] += 1

        if room["round_index"] == TOTAL_ROUNDS - 1:
            room["final_winner_id"] = max(
                room["players_order"],
                key=lambda pid: (
                    room["players"][pid]["wins"],
                    room["players"][pid]["total_score"],
                ),
            )


def parse_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    data = handler.rfile.read(length) if length else b"{}"
    try:
        return json.loads(data.decode("utf-8"))
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON body")


class Handler(BaseHTTPRequestHandler):
    server_version = "DrawDuel/0.1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
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
            suffix = target.suffix.lower()
            content_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
            }.get(suffix, "application/octet-stream")
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
                room, player_id = create_room(name)
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

        if parsed.path in {"/api/room/start", "/api/room/next", "/api/draw/update", "/api/draw/submit"}:
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
                        error_response(self, "Need two players to start")
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
                    if all(room["drawings"][pid]["submitted"] for pid in room["players_order"]):
                        room["stage"] = "judging"
                        room["judging_started_at"] = time.time()
                        threading.Thread(target=begin_judging, args=(room_code,), daemon=True).start()
                    json_response(self, sanitize_room(room, player_id))
                    return

        error_response(self, "Not found", HTTPStatus.NOT_FOUND)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Draw duel server running at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
