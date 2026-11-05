Crumble Hangout (multiplayer)

RUN IT
- Windows: double-click run.bat  -> http://localhost:8080
  (if C:\cloudflared\cloudflared.exe exists it also opens a public trycloudflare.com link for friends)
- Anywhere: pip install aiohttp, then python server.py
- Render: put everything in this folder at the top level of a GitHub repo (index.html, server.py, maps/, cookies/ ... not inside
  another folder), then on Render: New > Web Service > pick the repo. Runtime Python, Build "pip install -r requirements.txt",
  Start "python server.py" (PORT is picked up automatically). render.yaml has the same settings if you use New > Blueprint.

OWNER
- Type aD:YourName as your name to join as owner (cyan name + (OWNER)). aD: alone = epicfridge.
  If the owner is already online, other aD: logins are refused and the owner gets a warning.
- The prefix is the owner password: change OWNER_PREFIX at the top of server.py if you share this folder.
- O opens the owner menu: kick, Spawn Cake Hound, Coin Rain, Crystal Rain, Remove nonsense.
  Kicked players can rejoin straight away. Cake Hounds chase and bite (fling) cookies; any skill (1-4) next to one kills it.

SETTINGS AT THE TOP OF server.py
- MAX_PLAYERS (100), MAX_PER_IP (2), NAME_MAX (32), CHAT_MAX (150), HOUND_SPEED, SKILL_KILL_RANGE, RAIN_COUNT (100)

KEYS
WASD move, Shift run, Space jump, 1-8 actions, / chat, P hide chat, M menu (change cookie/map without respawning),
O owner menu, R reset camera, drag/scroll camera. Phones: turn on Mobile controls in the gear settings. Billboarding sprites (experimental) is in the gear settings.
