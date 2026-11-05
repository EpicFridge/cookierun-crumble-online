"""
Crumble Hangout - multiplayer server
Serves the game folder (index.html, maps/, cookies/, sounds/, ui/) and runs one room per map over WebSockets.

    pip install aiohttp
    python server.py            -> http://localhost:8080

Render / other hosts: start command "python server.py" (the PORT env var is respected).
"""
import asyncio, json, os, re, time, math, random, unicodedata
from aiohttp import web, WSMsgType

# ------------------------------------------------------------------ settings
PORT = int(os.environ.get("PORT", 8080))
MAX_PLAYERS = 100
MAX_PER_IP = 2                 # players per IP / network (the owner is exempt and not counted)
OWNER_PREFIX = "aD:"           # type  aD:Name  in the name box to join as owner (aD: alone = epicfridge). Treat it like a password.
OWNER_DEFAULT_NAME = "epicfridge"
NAME_MAX = 32
CHAT_MAX = 150
HOUND_SPEED = 3.3              # walking cookies (3.0) get caught, running ones (5.6) get away
HOUND_BITE_RANGE = 1.4
SKILL_KILL_RANGE = 3.0         # any skill (1-4) used this close to a Cake Hound kills it
MAX_HOUNDS = 40                # per map
RAIN_COUNT = 100
MAX_ITEMS = 600                # per map
TICK_HZ = 15                   # position snapshots per second, per map room

ROOT = os.path.dirname(os.path.abspath(__file__))


def read_list(path, key):
    try:
        s = open(os.path.join(ROOT, path), encoding="utf-8").read()
        return [e["id"] for e in json.loads(s.split("=", 1)[1].strip().rstrip(";"))]
    except Exception as e:
        print("could not read", path, e)
        return []


MAPS = set(read_list("maps/index.js", "mapList"))
COOKIES = set(read_list("cookies/index.js", "cookieList"))
DEFAULT_MAP = "mapthemeS0001" if "mapthemeS0001" in MAPS else (sorted(MAPS)[0] if MAPS else "")
DEFAULT_COOKIE = "cookie0001" if "cookie0001" in COOKIES else (sorted(COOKIES)[0] if COOKIES else "")
print(f"{len(MAPS)} maps, {len(COOKIES)} cookies")

# ------------------------------------------------------------------ state
players = {}       # id -> Player
hounds = {}        # map -> {hid: Hound}
items = {}         # map -> {iid: [kind, x, z]}
next_obj = 1
next_id = 1


class Player:
    def __init__(self, ws, ip):
        global next_id
        self.id = next_id; next_id += 1
        self.ws, self.ip = ws, ip
        self.name, self.owner = "", False
        self.cookie, self.map = DEFAULT_COOKIE, DEFAULT_MAP
        self.joined = False
        self.st = [0, 0, 0, 1, "idle", 0, 0, 1, 0]   # x y z face anim seq hidden size running
        self.chat_times = []
        self.wallet = {}
        self.since = time.time()
        self.safe_until = 0.0

    def public(self, for_owner=False):
        d = {"id": self.id, "name": self.name, "owner": self.owner, "cookie": self.cookie, "map": self.map}
        if for_owner:
            d["alt"] = sum(1 for p in players.values() if p.joined and p.ip == self.ip) > 1
            d["ipTag"] = abs(hash(self.ip)) % 10000
        return d


async def send(p, msg):
    if p.ws.closed: return
    try:
        await p.ws.send_str(msg if isinstance(msg, str) else json.dumps(msg, separators=(",", ":")))
    except Exception:
        pass


async def broadcast(msg, pred=lambda p: True):
    s = json.dumps(msg, separators=(",", ":"))
    await asyncio.gather(*(send(p, s) for p in list(players.values()) if p.joined and pred(p)), return_exceptions=True)


def counts():
    c = {}
    for p in players.values():
        if p.joined: c[p.map] = c.get(p.map, 0) + 1
    return c


async def send_roster():
    joined = sorted((p for p in players.values() if p.joined), key=lambda p: (not p.owner, p.since))
    base = {"t": "roster", "max": MAX_PLAYERS, "counts": counts()}
    plain = dict(base, players=[p.public() for p in joined])
    full = dict(base, players=[p.public(True) for p in joined])
    await asyncio.gather(*(send(p, full if p.owner else plain) for p in joined), return_exceptions=True)
    # people still in the menu (not joined) also get the map counts
    await asyncio.gather(*(send(p, {"t": "counts", "counts": base["counts"], "online": len(joined), "max": MAX_PLAYERS})
                           for p in players.values() if not p.joined), return_exceptions=True)


def clean_text(s, n):
    s = "".join(ch for ch in str(s) if unicodedata.category(ch)[0] != "C")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


def num(v, lo, hi, d=0.0):
    try:
        v = float(v)
        if math.isfinite(v): return max(lo, min(hi, v))
    except Exception:
        pass
    return d


async def send_items(p):
    room = items.get(p.map, {})
    await send(p, {"t": "items", "items": [[iid, it[0], it[1], it[2], 0] for iid, it in room.items()]})


class Hound:
    """An owner-spawned Cake Hound: chases the nearest cookie on its map, bites (skill0) and flings them away."""
    def __init__(self, mp, x, z):
        global next_obj
        self.id = next_obj; next_obj += 1
        self.map, self.x, self.z = mp, x, z
        self.f, self.anim, self.seq = 1, "spawn", 0
        self.state, self.timer, self.cool, self.target = "spawn", 0.4, 0.0, None

    def set_anim(self, a):
        self.anim = a; self.seq += 1

    def kill(self):
        self.state = "die"; self.timer = 1.3; self.set_anim("die")     # clients fling it into the air, then it's removed

    def step(self, dt, room_players):
        self.timer -= dt; self.cool -= dt
        if self.state == "die":
            return self.timer > 0          # False -> remove
        if self.state == "spawn":
            if self.timer <= 0: self.state = "chase"; self.set_anim("idle")
            return True
        if self.state == "bite":
            if not self.bitten and self.timer <= 0.3:
                self.bitten = True
                t = self.target
                if t and t.joined and not t.st[6] and time.time() > t.safe_until and math.hypot(t.st[0] - self.x, t.st[2] - self.z) < HOUND_BITE_RANGE + 0.6:
                    t.safe_until = time.time() + 1.8
                    dx, dz = t.st[0] - self.x, t.st[2] - self.z
                    d = math.hypot(dx, dz) or 1
                    asyncio.create_task(send(t, {"t": "fling", "dx": dx / d, "dz": dz / d}))
                    asyncio.create_task(broadcast({"t": "bite", "id": t.id, "x": round(t.st[0], 2), "z": round(t.st[2], 2)}, lambda q: q.map == self.map))
            if self.timer <= 0: self.state = "chase"; self.set_anim("idle")
            return True
        # chase the nearest visible cookie
        best, bd = None, 35.0
        for q in room_players:
            if q.st[6] or time.time() < q.safe_until: continue
            d = math.hypot(q.st[0] - self.x, q.st[2] - self.z)
            if d < bd: best, bd = q, d
        self.target = best
        if not best:
            if self.anim != "idle": self.set_anim("idle")
            return True
        dx, dz = best.st[0] - self.x, best.st[2] - self.z
        if bd < HOUND_BITE_RANGE and self.cool <= 0:
            self.state, self.timer, self.cool, self.bitten = "bite", 0.57, 1.3, False
            self.f = 1 if dx > 0 else -1
            self.set_anim("skill0")
            return True
        if bd > 0.9:
            step = min(bd - 0.8, HOUND_SPEED * dt)
            self.x += dx / bd * step; self.z += dz / bd * step
            if abs(dx) > 0.15: self.f = 1 if dx > 0 else -1
            if self.anim != "run": self.set_anim("run")
        return True


# ------------------------------------------------------------------ message handlers
async def handle_join(p, m):
    raw = str(m.get("name", ""))
    owner = raw.startswith(OWNER_PREFIX)
    name = clean_text(raw[len(OWNER_PREFIX):] if owner else raw, NAME_MAX)
    if owner and not name: name = OWNER_DEFAULT_NAME
    if not name:
        return await send(p, {"t": "error", "text": "Pick a name first."})
    if owner:
        others = [q for q in players.values() if q.joined and q.owner and q is not p]
        if others:
            if all(q.ip == p.ip for q in others):
                # the owner reconnecting from the same network: replace the old session
                for q in others:
                    q.joined = False
                    await broadcast({"t": "gone", "id": q.id})
                    try: await q.ws.close()
                    except Exception: pass
            else:
                for q in others:
                    await send(q, {"t": "warn", "text": f"Someone tried to join as owner (as \"{name}\")."})
                print(f"! owner login attempt from {p.ip} while owner is online")
                return await send(p, {"t": "error", "text": "Owner is already in game and has been notified."})
    if not owner and name.lower() == OWNER_DEFAULT_NAME.lower():
        return await send(p, {"t": "error", "text": "That name is reserved."})
    if any(q.joined and q.name.lower() == name.lower() for q in players.values()):
        return await send(p, {"t": "error", "text": "Someone online already has that name."})
    if not owner:
        if sum(1 for q in players.values() if q.joined) >= MAX_PLAYERS:
            return await send(p, {"t": "error", "text": f"The server is full ({MAX_PLAYERS}/{MAX_PLAYERS})."})
        if sum(1 for q in players.values() if q.joined and not q.owner and q.ip == p.ip) >= MAX_PER_IP:
            return await send(p, {"t": "error", "text": f"Only {MAX_PER_IP} players per network."})
    p.name, p.owner = name, owner
    p.cookie = m.get("cookie") if m.get("cookie") in COOKIES else DEFAULT_COOKIE
    p.map = m.get("map") if m.get("map") in MAPS else DEFAULT_MAP
    p.joined, p.since = True, time.time()
    await send(p, {"t": "welcome", "id": p.id, "name": p.name, "owner": p.owner, "max": MAX_PLAYERS})
    await send_items(p)
    await broadcast({"t": "sys", "text": f"{p.name} joined"})
    await send_roster()
    print(f"+ {p.name}{' (OWNER)' if owner else ''} [{p.ip}] -> {p.map}")


async def handle(p, m):
    t = m.get("t")
    if t == "join" and not p.joined:
        return await handle_join(p, m)
    if not p.joined:
        return
    if t == "st":
        s = m.get("s") or []
        if len(s) >= 9:
            old_anim, old_seq = p.st[4], p.st[5]
            p.st = [round(num(s[0], -500, 500), 3), round(num(s[1], -50, 200), 3), round(num(s[2], -500, 500), 3),
                    1 if num(s[3], -1, 1, 1) >= 0 else -1, str(s[4])[:16], int(num(s[5], 0, 1e9)), 1 if s[6] else 0,
                    round(num(s[7], 0.3, 2, 1), 2), 1 if s[8] else 0]
            if p.st[4].startswith("skill") and (p.st[4] != old_anim or p.st[5] != old_seq):
                for h in hounds.get(p.map, {}).values():
                    if h.state != "die" and math.hypot(h.x - p.st[0], h.z - p.st[2]) < SKILL_KILL_RANGE:
                        h.kill()
    elif t == "chat":
        text = clean_text(m.get("text", ""), CHAT_MAX)
        if not text: return
        now = time.time()
        p.chat_times = [x for x in p.chat_times if now - x < 6]
        if len(p.chat_times) >= 5 and not p.owner:
            return await send(p, {"t": "sys", "text": "Slow down a little!"})
        p.chat_times.append(now)
        await broadcast({"t": "chat", "id": p.id, "name": p.name, "owner": p.owner, "cookie": p.cookie, "map": p.map, "text": text})
    elif t == "jump":
        await broadcast({"t": "jump", "id": p.id}, lambda q: q.map == p.map and q is not p)
    elif t == "set":
        if m.get("cookie") in COOKIES and m["cookie"] != p.cookie:
            p.cookie = m["cookie"]; await send_roster()
    elif t == "map":
        if m.get("map") in MAPS and m["map"] != p.map:
            p.map = m["map"]; await send_roster(); await send_items(p)
    elif t == "kick" and p.owner:
        target = players.get(int(num(m.get("id"), 0, 1e9)))
        if target and target.joined and not target.owner:
            await send(target, {"t": "kicked", "by": p.name})
            await target.ws.close()
            await broadcast({"t": "sys", "text": f"{target.name} was kicked"})
    elif t == "grab":
        it = items.get(p.map, {}).pop(int(num(m.get("id"), 0, 1e12)), None)
        if it:
            p.wallet[it[0]] = p.wallet.get(it[0], 0) + 1
            await broadcast({"t": "itemsDel", "ids": [int(m["id"])]}, lambda q: q.map == p.map)
            await send(p, {"t": "wallet", "w": p.wallet})
    elif t == "hound" and p.owner:
        room = hounds.setdefault(p.map, {})
        if len(room) < MAX_HOUNDS:
            a = random.random() * math.tau
            h = Hound(p.map, p.st[0] + math.cos(a) * 7, p.st[2] + math.sin(a) * 7)
            room[h.id] = h
    elif t == "rain" and p.owner:
        kind = "crystal" if m.get("kind") == "crystal" else "coin"
        b = m.get("b") or [p.st[0] - 20, p.st[0] + 20, p.st[2] - 20, p.st[2] + 20]
        x0, x1, z0, z1 = (num(b[i], -500, 500) for i in range(4))
        room = items.setdefault(p.map, {})
        new = []
        global next_obj
        for _ in range(RAIN_COUNT):
            if len(room) >= MAX_ITEMS: break
            iid = next_obj; next_obj += 1
            x, z = round(random.uniform(x0 + 1, x1 - 1), 2), round(random.uniform(z0 + 1, z1 - 1), 2)
            room[iid] = [kind, x, z]
            new.append([iid, kind, x, z, round(random.random() * 3, 2)])
        await broadcast({"t": "itemsAdd", "items": new}, lambda q: q.map == p.map)
    elif t == "clear" and p.owner:
        hounds.clear(); items.clear()
        await broadcast({"t": "clearAll"})
        await broadcast({"t": "sys", "text": "The owner removed all the nonsense."})


# ------------------------------------------------------------------ websocket + loop
def client_ip(request):
    fwd = request.headers.get("X-Forwarded-For", "") or request.headers.get("CF-Connecting-IP", "")
    if fwd: return fwd.split(",")[0].strip()
    return request.remote or "?"


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8192)
    await ws.prepare(request)
    ip = client_ip(request)
    p = Player(ws, ip)
    players[p.id] = p
    await send(p, {"t": "counts", "counts": counts(), "online": sum(1 for q in players.values() if q.joined), "max": MAX_PLAYERS})
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT: continue
            try: m = json.loads(msg.data)
            except Exception: continue
            if isinstance(m, dict): await handle(p, m)
    finally:
        was = p.joined
        players.pop(p.id, None)
        if was:
            await broadcast({"t": "sys", "text": f"{p.name} left"})
            await broadcast({"t": "gone", "id": p.id})
            await send_roster()
            print(f"- {p.name}")
    return ws


async def ticker(app):
    async def loop():
        while True:
            await asyncio.sleep(1 / TICK_HZ)
            rooms = {}
            for p in players.values():
                if p.joined: rooms.setdefault(p.map, []).append(p)
            for mp in list(hounds):
                hs = hounds[mp]
                for hid in [hid for hid, h in hs.items() if not h.step(1 / TICK_HZ, rooms.get(mp, []))]:
                    del hs[hid]
            for mp, room in rooms.items():
                hs = hounds.get(mp, {})
                msg = json.dumps({"t": "snap", "p": [[q.id] + q.st for q in room],
                                  "h": [[h.id, round(h.x, 2), round(h.z, 2), h.f, h.anim, h.seq] for h in hs.values()]}, separators=(",", ":"))
                await asyncio.gather(*(send(q, msg) for q in room), return_exceptions=True)
    task = asyncio.create_task(loop())
    yield
    task.cancel()


async def index(request):
    return web.FileResponse(os.path.join(ROOT, "index.html"), headers={"Cache-Control": "no-cache"})


@web.middleware
async def cache_headers(request, handler):
    resp = await handler(request)
    if request.path.startswith(("/maps/", "/cookies/", "/enemies/", "/sounds/", "/ui/")):
        resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


app = web.Application(middlewares=[cache_headers])
app.cleanup_ctx.append(ticker)
app.router.add_get("/", index)
app.router.add_get("/index.html", index)
app.router.add_get("/ws", ws_handler)
for d in ("maps", "cookies", "enemies", "sounds", "ui"):
    if os.path.isdir(os.path.join(ROOT, d)):
        app.router.add_static(f"/{d}/", os.path.join(ROOT, d))

if __name__ == "__main__":
    print(f"Crumble Hangout on http://localhost:{PORT}")
    web.run_app(app, port=PORT, print=None)
