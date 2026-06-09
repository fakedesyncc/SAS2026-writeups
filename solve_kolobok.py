#!/usr/bin/env python3

import re
import secrets
import sys
import time
from collections import deque

import requests


BASE_URL = "https://kolobok.task.sasc.tf"
DIRS = [(-1, 0), (1, 0), (0, -1), (0, 1)]
ENEMY_TILES = {"R", "F", "E"}


class KolobokClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def register_and_login(self):
        username = "exp" + secrets.token_hex(5)
        password = "p" + secrets.token_hex(5)
        self.session.post(
            f"{self.base_url}/register",
            data={"username": username, "password": password},
            allow_redirects=False,
            timeout=10,
        )
        self.session.post(
            f"{self.base_url}/login",
            data={"username": username, "password": password},
            allow_redirects=False,
            timeout=10,
        )
        return username, password

    def game_state(self):
        return self.session.get(f"{self.base_url}/game_state", timeout=10).json()

    def move(self, dx, dy):
        return self.session.post(
            f"{self.base_url}/move_manual",
            json={"dx": dx, "dy": dy},
            timeout=10,
        ).json()

    def submit_kernel(self, code):
        return self.session.post(
            f"{self.base_url}/submit_kernel",
            data={"kernel_input": code},
            timeout=10,
        ).json()

    def get_flag(self):
        return self.session.get(f"{self.base_url}/get_flag", timeout=10).json()


def update_known(known, state):
    enemies = set()
    stars = set()
    exits = set()

    for cell in state.get("visible", []):
        x, y, tile = cell["x"], cell["y"], cell["t"]
        if tile in ENEMY_TILES:
            enemies.add((x, y))
            tile = " "
        elif tile == "P":
            tile = " "
        elif tile == "S":
            stars.add((x, y))
        elif tile == "X":
            exits.add((x, y))
        known[(x, y)] = tile

    return enemies, stars, exits


def bfs(start, goals, known, enemies, allow_exit_goal=False, strict=True):
    if not goals:
        return None

    blocked = set(enemies)
    if strict:
        for ex, ey in enemies:
            for dx, dy in DIRS + [(0, 0)]:
                blocked.add((ex + dx, ey + dy))

    queue = deque([start])
    prev = {start: None}

    while queue:
        pos = queue.popleft()
        if pos in goals and pos != start:
            path = []
            while pos != start:
                path.append(pos)
                pos = prev[pos]
            path.reverse()
            return path

        for dx, dy in DIRS:
            nxt = (pos[0] + dx, pos[1] + dy)
            if nxt in prev:
                continue
            if not (0 <= nxt[0] < 20 and 0 <= nxt[1] < 20):
                continue

            tile = known.get(nxt)
            if tile is None or tile == "#":
                continue
            if tile == "X" and not allow_exit_goal and nxt not in goals:
                continue
            if nxt in blocked and nxt not in goals:
                continue

            prev[nxt] = pos
            queue.append(nxt)

    return None


def frontier_cells(known):
    frontiers = set()
    for (x, y), tile in known.items():
        if tile in {"#", "X"}:
            continue
        for dx, dy in DIRS:
            nxt = (x + dx, y + dy)
            if 0 <= nxt[0] < 20 and 0 <= nxt[1] < 20 and nxt not in known:
                frontiers.add((x, y))
                break
    return frontiers


def stop_kernel(client):
    client.submit_kernel(
        "def player_kernel(mapdata_ref, auxdata_ref, out_ref):\n"
        "    1/0\n"
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        data = client.game_state()
        if not data.get("running"):
            return data
        time.sleep(0.1)
    return client.game_state()


def wait_for_tick(client, previous_step, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.game_state()
        step = data.get("state", {}).get("step")
        if step != previous_step:
            return data
        time.sleep(0.1)
    return client.game_state()


def dump_hidden_key(client):
    state = client.game_state()["state"]
    prev_step = state["step"]
    code = (
        "def player_kernel(mapdata_ref, auxdata_ref, out_ref):\n"
        "    i = 0\n"
        "    while i < 8:\n"
        "        auxdata_ref[i] = auxdata_ref[i + 56]\n"
        "        i = i + 1\n"
        "    out_ref[0] = 0\n"
    )
    client.submit_kernel(code)
    data = wait_for_tick(client, prev_step)
    key = bytes(data["state"]["aux_data"][:8])
    stop_kernel(client)
    return key


def dump_map_repr(client, chunk_size=56, max_len=512):
    chunks = []
    for offset in range(0, max_len, chunk_size):
        state = client.game_state()["state"]
        prev_step = state["step"]
        code = (
            "def player_kernel(mapdata_ref, auxdata_ref, out_ref):\n"
            "    s = str(mapdata_ref)\n"
            "    i = 0\n"
            f"    while i < {chunk_size}:\n"
            f"        j = i + {offset}\n"
            "        if j < len(s):\n"
            "            auxdata_ref[i] = ord(s[j])\n"
            "        else:\n"
            "            auxdata_ref[i] = 0\n"
            "        i = i + 1\n"
            "    out_ref[0] = 0\n"
        )
        client.submit_kernel(code)
        data = wait_for_tick(client, prev_step)
        raw = data["state"]["aux_data"][:chunk_size]
        chunk = "".join(chr(x) for x in raw if x != 0)
        chunks.append(chunk)
        stop_kernel(client)
        if len(chunk) < chunk_size:
            break
    return "".join(chunks)


def parse_lock(map_repr):
    match = re.search(r"LOCK=([0-9a-f]+)", map_repr)
    if not match:
        raise RuntimeError(f"LOCK not found in map repr: {map_repr!r}")
    return bytes.fromhex(match.group(1))


def decode_lock(lock_bytes, key_bytes):
    plain = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(lock_bytes))
    text = plain.decode("ascii")

    match = re.search(
        r"\|A=(\d+);B=(\d+);C=(\d+)\|K1=([^;]+);K2=([^;]+);K3=([^|]+)\|SLOTS=([\d,]+)",
        text,
    )
    if not match:
        raise RuntimeError(f"Unexpected decrypted lock: {text!r}")

    a, b, c = (int(match.group(i)) for i in range(1, 4))
    exprs = {
        "K1": match.group(4),
        "K2": match.group(5),
        "K3": match.group(6),
    }
    slots = [int(x) for x in match.group(7).split(",")]

    env = {"A": a, "B": b, "C": c}
    values = []
    for name in ("K1", "K2", "K3"):
        expr = exprs[name]
        if not re.fullmatch(r"[A-Z0-9+*]+", expr):
            raise RuntimeError(f"Unsafe expression: {expr}")
        value = eval(expr, {"__builtins__": {}}, env)
        env[name] = value
        values.append(value)

    return text, slots, values


def unlock_maintenance(client, slots, values):
    lines = ["def player_kernel(mapdata_ref, auxdata_ref, out_ref):"]
    for slot, value in zip(slots, values):
        lines.append(f"    auxdata_ref[{slot}] = {value}")
    lines.append("    out_ref[0] = 0")
    client.submit_kernel("\n".join(lines) + "\n")

    state = client.game_state()["state"]
    prev_step = state["step"]
    data = wait_for_tick(client, prev_step)
    if not data["state"].get("maintenance_open"):
        raise RuntimeError("Maintenance did not unlock")
    stop_kernel(client)
    return data


def collect_stars(client, max_steps=1800):
    known = {}
    last_collected = -1
    discovered_exits = set()

    state = client.game_state()["state"]
    for _ in range(max_steps):
        enemies, stars, exits = update_known(known, state)
        discovered_exits |= exits

        pos = tuple(state["player_pos"])
        collected = state["scales_collected"]
        if collected != last_collected:
            last_collected = collected
            print(f"[collect] stars={collected}/8 pos={pos} step={state['step']}")

        message = state.get("message")
        if message in {"Try again", "Nice try, you almost got the flag :)"}:
            raise RuntimeError(f"Run failed: {message}")

        if collected >= 8:
            return known, discovered_exits, state

        path = None
        if stars:
            path = bfs(pos, stars, known, enemies, strict=True)
            if path is None:
                path = bfs(pos, stars, known, enemies, strict=False)
        if path is None:
            frontiers = frontier_cells(known)
            path = bfs(pos, frontiers, known, enemies, strict=True)
            if path is None:
                path = bfs(pos, frontiers, known, enemies, strict=False)
        if not path:
            raise RuntimeError("No path to star/frontier")

        nxt = path[0]
        moved = client.move(nxt[0] - pos[0], nxt[1] - pos[1])
        state = moved["state"]
        time.sleep(0.01)

    raise RuntimeError("Step budget exhausted while collecting stars")


def escape_after_unlock(client, known, max_steps=1200):
    state = client.game_state()["state"]
    for _ in range(max_steps):
        enemies, _, exits = update_known(known, state)
        pos = tuple(state["player_pos"])

        if state.get("escaped"):
            return client.get_flag()["flag"]
        if client.game_state().get("escaped"):
            return client.get_flag()["flag"]

        path = None
        known_exits = {cell for cell, tile in known.items() if tile == "X"}
        if exits:
            known_exits |= exits
        if known_exits:
            path = bfs(pos, known_exits, known, enemies, allow_exit_goal=True, strict=True)
            if path is None:
                path = bfs(pos, known_exits, known, enemies, allow_exit_goal=True, strict=False)
        if path is None:
            frontiers = frontier_cells(known)
            path = bfs(pos, frontiers, known, enemies, strict=True)
            if path is None:
                path = bfs(pos, frontiers, known, enemies, strict=False)
        if not path:
            raise RuntimeError("No path to exit/frontier after maintenance unlock")

        nxt = path[0]
        moved = client.move(nxt[0] - pos[0], nxt[1] - pos[1])
        state = moved["state"]
        if moved.get("escaped"):
            return client.get_flag()["flag"]

        message = state.get("message")
        if message in {"Try again", "Nice try, you almost got the flag :)"}:
            raise RuntimeError(f"Unexpected failure after unlock: {message}")

        time.sleep(0.01)

    raise RuntimeError("Step budget exhausted after maintenance unlock")


def solve(base_url):
    for attempt in range(1, 25):
        client = KolobokClient(base_url)
        username, password = client.register_and_login()
        print(f"[attempt {attempt}] {username} / {password}")

        try:
            known, _, state = collect_stars(client)
            print(f"[attempt {attempt}] collected 8 stars at step {state['step']}")

            key = dump_hidden_key(client)
            map_repr = dump_map_repr(client)
            lock = parse_lock(map_repr)
            decoded, slots, values = decode_lock(lock, key)
            print(f"[attempt {attempt}] key={key.hex()}")
            print(f"[attempt {attempt}] decoded={decoded}")
            print(f"[attempt {attempt}] slots={slots} values={values}")

            unlock_maintenance(client, slots, values)
            print(f"[attempt {attempt}] maintenance unlocked")

            flag = escape_after_unlock(client, known)
            return flag
        except Exception as exc:
            print(f"[attempt {attempt}] failed: {exc}")
            continue

    raise RuntimeError("All attempts failed")


def main():
    base_url = BASE_URL
    if len(sys.argv) > 1:
        base_url = sys.argv[1]

    flag = solve(base_url)
    print(flag)


if __name__ == "__main__":
    main()
