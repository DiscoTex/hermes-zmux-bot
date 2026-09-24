#!/usr/bin/env python3
"""
Discord bot that drives a dfrotz Z-machine process per (guild, channel)
with an auto-generated ASCII mapper.

Only messages that start with '>' are treated as game commands.
Game output is posted verbatim inside a code block. Mapper commands:
    >map / >showmap  – show current ASCII map
    >new             – kill the current session and start fresh
    >load            – same as >new but tries to restore from save
"""

import asyncio
import io
import json
import os
import re
import shutil
import sys
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import discord

# ---------- CONFIG ----------
INTENTS = discord.Intents.default()
INTENTS.message_content = True
CLIENT = discord.Client(intents=INTENTS)

_HERE = Path(__file__).resolve().parent

BASE_DIR = _HERE / "saves"
BASE_DIR.mkdir(parents=True, exist_ok=True)

GAMES_DIR = _HERE / "games"
GAMES_DIR.mkdir(parents=True, exist_ok=True)

def list_games() -> dict[str, Path]:
    """Return a dict of lowercase short name -> Path for all z-machine files in GAMES_DIR."""
    games = {}
    for p in sorted(GAMES_DIR.glob("*")):
        if p.suffix.lower() in {f".z{i}" for i in range(1, 9)} or p.suffix.lower() == ".zblorb":
            games[p.stem.lower()] = p
    return games

def dfrotz_cmd(game_path: Path) -> list[str]:
    return [
        "/usr/games/dfrotz",
        "-m",        # disable MORE prompts
        "-q",        # quiet startup banner
        "-w", "200", # wide to avoid line-wrapping
        str(game_path),
    ]

PROMPT_RE = re.compile(r"(?m)^\s*>\s*$")

# Detect "Exits:" / "Obvious exits:" lines in game output
EXITS_LINE_RE = re.compile(
    r"^(?:Obvious exits?|Exits?)\s*:\s*(.+?)\.?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Individual direction words inside the exits list
DIR_WORD_RE = re.compile(
    r"\b(north|south|east|west|northeast|northwest|southeast|southwest|up|down)\b",
    re.IGNORECASE,
)

# ---------- MAPPER ----------
class Room:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.exits: dict[str, str] = {}

class GameMap:
    def __init__(self):
        self.rooms: dict[str, Room] = {}
        self.start: str | None = None
        self.here: str | None = None
        self.pending: set[str] = {}   # temp-named rooms waiting for real name
        self._temp_counter: int = 0

    def _next_temp_name(self) -> str:
        import string
        # Generate RoomA, RoomB, ..., RoomZ, RoomAA, RoomAB, ...
        letters = string.ascii_uppercase
        n = self._temp_counter
        self._temp_counter += 1
        name = ""
        while True:
            name = letters[n % 26] + name
            n = n // 26 - 1
            if n < 0:
                break
        return "Room" + name

    def rename_room(self, old_name: str, new_name: str):
        """Rename a room in-place, updating all exit references."""
        if old_name not in self.rooms or old_name == new_name:
            return
        room = self.rooms.pop(old_name)
        room.name = new_name
        self.rooms[new_name] = room
        # Update all exit references pointing to old_name
        for r in self.rooms.values():
            for d, dst in list(r.exits.items()):
                if dst == old_name:
                    r.exits[d] = new_name
        if self.here == old_name:
            self.here = new_name
        if self.start == old_name:
            self.start = new_name
        if old_name in self.pending:
            self.pending.discard(old_name)
            self.pending.add(new_name)

    def mark_temp(self, query: str) -> list[str]:
        """Rename rooms matching query to RoomA/B/... and mark pending."""
        q = query.lower()
        targets = [n for n in list(self.rooms) if q in n.lower()]
        renamed = []
        for name in targets:
            temp = self._next_temp_name()
            self.rename_room(name, temp)
            self.pending.add(temp)
            renamed.append(f"{name} → {temp}")
        return renamed

    def add_room(self, name: str, description: str) -> "Room":
        if name not in self.rooms:
            self.rooms[name] = Room(name, description)
            if self.start is None:
                self.start = self.here = name
        return self.rooms[name]

    def set_exit(self, src: str, direction: str, dst: str):
        if src not in self.rooms:
            self.add_room(src, "")
        self.rooms[src].exits[direction] = dst

    def delete_room(self, query: str) -> list[str]:
        """Remove rooms whose names contain `query` (case-insensitive).
        Cleans up all exit references. Returns list of deleted room names."""
        q = query.lower()
        targets = [n for n in list(self.rooms) if q in n.lower()]
        for name in targets:
            del self.rooms[name]
            for room in self.rooms.values():
                room.exits = {d: dst for d, dst in room.exits.items() if dst != name}
            if self.here == name:
                self.here = None
            if self.start == name:
                self.start = next(iter(self.rooms), None)
        if targets:
            pass  # caller is responsible for saving the map
        return targets

    def to_image(self) -> bytes | None:
        """Render the map as a PNG (bytes). Returns None if no data yet."""
        if not self.start or self.start not in self.rooms:
            return None

        def is_real_room(name: str) -> bool:
            if len(name) > 30: return False
            if any(c in name for c in ('"', '[', '/', '.', "'", '!')): return False
            if not name[0].isupper(): return False
            return True

        destinations: set[str] = set()
        for info in self.rooms.values():
            destinations.update(info.exits.values())
        real_rooms = {k: v for k, v in self.rooms.items()
                      if is_real_room(k) and (k in destinations or v.exits)}

        GRID_DELTA = {
            "north":     (0,  1), "south":     (0, -1),
            "east":      (1,  0), "west":      (-1, 0),
            "northeast": (1,  1), "northwest": (-1, 1),
            "southeast": (1, -1), "southwest": (-1,-1),
            "up":        (0,  3), "down":      (0, -3),
        }
        SHORT = {"north":"N","south":"S","east":"E","west":"W",
                 "up":"U","down":"D","northeast":"NE","northwest":"NW",
                 "southeast":"SE","southwest":"SW"}

        root = self.start if self.start in real_rooms else (next(iter(real_rooms)) if real_rooms else None)
        if not root:
            return None

        grid: dict[str, tuple[int, int]] = {root: (0, 0)}
        occupied: dict[tuple[int, int], str] = {(0, 0): root}
        q: deque[str] = deque([root])
        while q:
            cur = q.popleft()
            cx, cy = grid[cur]
            for direction, dst in real_rooms.get(cur, Room("","")).exits.items():
                if dst not in real_rooms or dst in grid:
                    continue
                dx, dy = GRID_DELTA.get(direction, (0, 0))
                candidate = (cx + dx, cy + dy)
                # Resolve collision: push rooms in the direction of travel to make space
                for _attempt in range(50):
                    if candidate not in occupied or occupied[candidate] == dst:
                        break
                    new_grid: dict[str, tuple[int, int]] = {}
                    new_occ:  dict[tuple[int, int], str] = {}
                    for rname, (rx, ry) in grid.items():
                        # Shift rooms that are "ahead" in the direction of travel
                        nx = rx + (1 if dx > 0 and rx >= candidate[0] else
                                  -1 if dx < 0 and rx <= candidate[0] else 0)
                        ny = ry + (1 if dy > 0 and ry >= candidate[1] else
                                  -1 if dy < 0 and ry <= candidate[1] else 0)
                        new_grid[rname] = (nx, ny)
                        new_occ[(nx, ny)] = rname
                    grid = new_grid
                    occupied = new_occ
                    cx, cy = grid[cur]
                    candidate = (cx + dx, cy + dy)
                else:
                    # fallback: park it at the far edge in the travel direction
                    if dx != 0:
                        fx = (max(x for x, y in grid.values()) + 1) if dx > 0 \
                             else (min(x for x, y in grid.values()) - 1)
                        candidate = (fx, cy + dy)
                    else:
                        fy = (max(y for x, y in grid.values()) + 1) if dy > 0 \
                             else (min(y for x, y in grid.values()) - 1)
                        candidate = (cx + dx, fy)
                grid[dst] = candidate
                occupied[candidate] = dst
                q.append(dst)

        off = max((x for x, y in grid.values()), default=0) + 3
        for i, r in enumerate(r for r in real_rooms if r not in grid):
            grid[r] = (off + i, 0)

        edges = []
        seen: set[frozenset[str]] = set()
        for name, room in real_rooms.items():
            for direction, dst in room.exits.items():
                if dst not in real_rooms or dst not in grid or name not in grid:
                    continue
                pair: frozenset[str] = frozenset([name, dst])
                if pair in seen:
                    continue
                seen.add(pair)
                edges.append((name, dst, direction))

        CELL  = 130
        PAD   = 70
        BOX_W = 116
        BOX_H = 38
        BG         = (35, 39, 42)
        ROOM_FILL  = (44, 47, 51)
        ROOM_HERE  = (114, 137, 218)
        BORDER_COL = (114, 137, 218)
        EDGE_COL   = (120, 140, 160)
        TEXT_COL   = (255, 255, 255)
        LABEL_COL  = (160, 180, 200)
        UD_COL     = (200, 160, 100)

        xs = [x for x, y in grid.values()]
        ys = [y for x, y in grid.values()]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

        W = (max_x - min_x) * CELL + PAD * 2 + BOX_W
        H = (max_y - min_y) * CELL + PAD * 2 + BOX_H

        def to_px(gx: int, gy: int) -> tuple[int, int]:
            x = (gx - min_x) * CELL + PAD + BOX_W // 2
            y = (max_y - gy) * CELL + PAD + BOX_H // 2
            return x, y

        img = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img)

        try:
            font    = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
            font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
        except Exception:
            font = font_sm = ImageFont.load_default()

        STUB = 18  # px stub length for misaligned connections
        DIR_VEC = {
            "north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0),
            "northeast": (1, -1), "northwest": (-1, -1),
            "southeast": (1, 1), "southwest": (-1, 1),
            "up": (0, -1), "down": (0, 1),
        }
        for src, dst, direction in edges:
            x1, y1 = to_px(*grid[src])
            x2, y2 = to_px(*grid[dst])
            col   = UD_COL if direction in ("up", "down") else EDGE_COL
            width = 3 if direction in ("up", "down") else 2
            gx1, gy1 = grid[src]
            gx2, gy2 = grid[dst]
            dx, dy = abs(gx2 - gx1), abs(gy2 - gy1)
            # If rooms are adjacent (1 cell apart), draw a direct line
            if max(dx, dy) <= 1:
                draw.line([(x1, y1), (x2, y2)], fill=col, width=width)
                mx, my = (x1 + x2) // 2, (y1 + y2) // 2
            else:
                # Draw an L-shaped route: horizontal then vertical
                mid = (x2, y1)
                draw.line([(x1, y1), mid], fill=col, width=width)
                draw.line([mid, (x2, y2)], fill=col, width=width)
                mx, my = (x1 + x2) // 2, y1
            label = SHORT.get(direction, direction)
            bbox = draw.textbbox((0, 0), label, font=font_sm)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.rectangle([mx - 2, my - 2, mx + tw + 4, my + th + 2], fill=BG)
            draw.text((mx, my), label, fill=col, font=font_sm)

        for name, (gx, gy) in grid.items():
            cx, cy = to_px(gx, gy)
            x0, y0 = cx - BOX_W // 2, cy - BOX_H // 2
            x1, y1 = cx + BOX_W // 2, cy + BOX_H // 2
            fill   = ROOM_HERE if name == self.here else ROOM_FILL
            border = (255, 255, 255) if name == self.here else BORDER_COL
            bw     = 3 if name == self.here else 2
            # Temp/pending rooms get an orange dashed-look via a distinct color
            if name in self.pending:
                fill   = (80, 50, 20) if name != self.here else ROOM_HERE
                border = (255, 165, 0)
                bw     = 2
            draw.rounded_rectangle([x0, y0, x1, y1], radius=7, fill=fill, outline=border, width=bw)
            lbl = name if len(name) <= 14 else name[:13] + "…"
            bbox = draw.textbbox((0, 0), lbl, font=font)
            tw = bbox[2] - bbox[0]
            draw.text((cx - tw // 2, cy - 8), lbl, fill=TEXT_COL, font=font)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def to_dict(self):
        return {
            "start": self.start,
            "here": self.here,
            "pending": list(self.pending),
            "temp_counter": self._temp_counter,
            "rooms": {
                name: {"description": r.description, "exits": r.exits}
                for name, r in self.rooms.items()
            },
        }

    @classmethod
    def from_dict(cls, data):
        g = cls()
        g.start = data.get("start")
        g.here = data.get("here")
        g.pending = set(data.get("pending", []))
        g._temp_counter = data.get("temp_counter", 0)
        for name, info in data.get("rooms", {}).items():
            r = Room(name, info.get("description", ""))
            r.exits = info.get("exits", {})
            g.rooms[name] = r
        return g

# ---------- SESSION ----------
class GameSession:
    """Manages one dfrotz process for a (guild, channel) pair."""

    def __init__(self, guild_id: int, channel_id: int, game_path: Path):
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.game_path = game_path
        self.game_name = game_path.stem.lower()
        self.proc: asyncio.subprocess.Process | None = None
        self._save_prefix = f"{guild_id}_{channel_id}_{self.game_name}"
        self.save_path = BASE_DIR / f"{self._save_prefix}.qzl"  # default/quicksave slot
        self.map_path = BASE_DIR / f"{self._save_prefix}.map.json"
        self.game_map = self._load_map()
        self._active_slot: str | None = None  # which named slot is loaded

    def named_save_path(self, slot: str) -> Path:
        """Return the path for a named save slot (alphanumeric + underscores only)."""
        safe = re.sub(r"[^a-z0-9_]", "_", slot.lower())[:32]
        return BASE_DIR / f"{self._save_prefix}.{safe}.qzl"

    def named_map_path(self, slot: str) -> Path:
        """Return the map snapshot path for a named save slot."""
        safe = re.sub(r"[^a-z0-9_]", "_", slot.lower())[:32]
        return BASE_DIR / f"{self._save_prefix}.{safe}.map.json"

    def list_saves(self) -> list[str]:
        """Return list of slot names available for this game/channel."""
        slots = []
        # default quicksave
        if self.save_path.is_file():
            slots.append("(quicksave)")
        # named slots
        for p in sorted(BASE_DIR.glob(f"{self._save_prefix}.*.qzl")):
            slot = p.stem[len(self._save_prefix) + 1:]  # strip prefix and leading dot
            slots.append(slot)
        return slots

    def _load_map(self) -> GameMap:
        if self.map_path.is_file():
            try:
                return GameMap.from_dict(
                    json.loads(self.map_path.read_text(encoding="utf-8"))
                )
            except Exception:
                pass
        return GameMap()

    def _save_map(self):
        data = json.dumps(self.game_map.to_dict(), indent=2)
        try:
            self.map_path.write_text(data, encoding="utf-8")
        except Exception:
            pass
        # Also keep the active named slot map in sync
        if self._active_slot:
            try:
                self.named_map_path(self._active_slot).write_text(data, encoding="utf-8")
            except Exception:
                pass

    async def start(self, restore: bool = True):
        """Start dfrotz, optionally restoring from save."""
        self.proc = await asyncio.create_subprocess_exec(
            *dfrotz_cmd(self.game_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        # Read startup banner / first prompt
        await self._read_until_prompt(timeout=3.0)

        if restore:
            # Prefer the active named slot over the quicksave
            if self._active_slot:
                slot_path = self.named_save_path(self._active_slot)
                restore_path = slot_path if slot_path.is_file() else self.save_path
            else:
                restore_path = self.save_path
            if restore_path.is_file():
                await self._send("restore")
                await self._read_until_prompt(timeout=2.0)  # filename prompt
                await self._send(str(restore_path))
                await self._read_until_prompt(timeout=2.0)  # "Ok." + next prompt
                # Sync map location after restore
                await self._send("look")
                look_out = await self._read_until_prompt(timeout=3.0)
                self.update_map(look_out, None)

    async def _send(self, cmd: str):
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((cmd + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def _read_until_prompt(self, timeout: float = 2.0) -> str:
        """Read dfrotz stdout until we see the '>' prompt or timeout."""
        chunks: list[str] = []
        deadline = asyncio.get_event_loop().time() + timeout
        assert self.proc and self.proc.stdout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(
                    self.proc.stdout.read(4096), timeout=min(remaining, 0.1)
                )
                if not chunk:
                    break  # EOF
                text = chunk.decode("utf-8", errors="replace")
                chunks.append(text)
                if PROMPT_RE.search(text):
                    break
            except asyncio.TimeoutError:
                # No data in this slice; keep looping until deadline
                if asyncio.get_event_loop().time() >= deadline:
                    break

        full = "".join(chunks)
        # Strip the prompt itself from the returned text
        return re.split(PROMPT_RE, full, maxsplit=1)[0]

    async def send_command(self, cmd: str) -> str:
        """Send a command and return the game's response text."""
        if self.proc is None or self.proc.returncode is not None:
            await self.start(restore=True)

        await self._send(cmd)
        output = await self._read_until_prompt(timeout=3.0)

        # Handle save command extras (filename + overwrite prompts)
        if cmd.lower() == "save":
            await self._send(str(self.save_path))
            extra = await self._read_until_prompt(timeout=2.0)
            if "overwrite" in extra.lower() or "replace" in extra.lower():
                await self._send("y")
                extra += await self._read_until_prompt(timeout=2.0)
            output += extra

        return output

    async def save_to_slot(self, slot: str) -> str:
        """Save to a named slot. Returns status message."""
        path = self.named_save_path(slot)
        if self.proc is None or self.proc.returncode is not None:
            await self.start(restore=True)
        await self._send("save")
        await self._read_until_prompt(timeout=2.0)  # filename prompt
        await self._send(str(path))
        extra = await self._read_until_prompt(timeout=2.0)
        if "overwrite" in extra.lower() or "replace" in extra.lower():
            await self._send("y")
            extra += await self._read_until_prompt(timeout=2.0)
        # Snapshot the current map alongside the save
        shutil.copy2(self.map_path, self.named_map_path(slot))
        self._active_slot = slot  # keep map in sync going forward
        return f"Saved to slot '{slot}'."

    async def load_from_slot(self, slot: str) -> str:
        """Load from a named slot. Returns game output after restore."""
        path = self.named_save_path(slot)
        if not path.is_file():
            return f"No save found for slot '{slot}'."
        self.kill()
        self.proc = await asyncio.create_subprocess_exec(
            *dfrotz_cmd(self.game_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await self._read_until_prompt(timeout=3.0)
        await self._send("restore")
        await self._read_until_prompt(timeout=2.0)
        await self._send(str(path))
        output = await self._read_until_prompt(timeout=2.0)
        # Restore the map snapshot for this slot if it exists
        slot_map = self.named_map_path(slot)
        if slot_map.is_file():
            shutil.copy2(slot_map, self.map_path)
            self.game_map = self._load_map()
        self._active_slot = slot  # track so _save_map stays in sync
        # Issue a silent "look" to sync the map's current location without costing a turn
        await self._send("look")
        look_output = await self._read_until_prompt(timeout=3.0)
        self.update_map(look_output, None)
        return (output + "\n" + look_output).strip() or f"Loaded slot '{slot}'."

    # Opposite directions for bidirectional exit recording
    OPPOSITE_DIR = {
        "north": "south", "south": "north",
        "east": "west",   "west": "east",
        "up": "down",     "down": "up",
        "northeast": "southwest", "southwest": "northeast",
        "northwest": "southeast", "southeast": "northwest",
    }

    def update_map(self, output: str, direction: str | None):
        """Parse output and update the map."""
        lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
        if not lines:
            return

        # Bail out on failed movement — don't add junk rooms or break directionality
        FAILED_MOVE_PHRASES = (
            "you can't go that way", "you can't go there",
            "that way is blocked", "there is no exit",
            "you cannot go", "that's not a direction",
            "you bump into", "there's no way",
            "certain death",
        )
        first_lower = lines[0].lower()
        if direction and any(p in first_lower for p in FAILED_MOVE_PHRASES):
            return

        # Skip "You travel..." / "You are..." lines to find the actual room name
        name_line = lines[0]
        if name_line.lower().startswith("you"):
            for ln in lines[1:]:
                if ln and not ln.startswith(">"):
                    name_line = ln
                    break

        # Normalize: "Dorm, in the bed" → "Dorm" (sub-location, not a new room)
        raw_name = name_line
        room_name = raw_name.split(",")[0].strip()

        description = ""
        for ln in lines[1:]:
            if ln and not ln.startswith(">"):
                description = ln[:80]
                break

        current_room = self.game_map.add_room(room_name, description)

        # Auto-correct temp-named room if we've arrived here and know the real name
        if self.game_map.here and self.game_map.here in self.game_map.pending and direction:
            # We moved into the pending room — it already exists under its temp name.
            # But we need to check: did we just land ON the pending room, or are we arriving fresh?
            pass  # handled below after here is updated

        if direction and self.game_map.here:
            prev = self.game_map.here
            # Check if the room we moved into is a pending temp room
            temp_name = self.game_map.rooms.get(prev, Room("","")).exits.get(direction)
            if temp_name and temp_name in self.game_map.pending and temp_name != room_name:
                # We've arrived at a temp room; real name is room_name — rename it
                self.game_map.rename_room(temp_name, room_name)
                self.game_map.pending.discard(room_name)
                # current_room now refers to old temp Room object under wrong name; re-fetch
                current_room = self.game_map.rooms.get(room_name) or current_room
            if current_room.name != prev:
                self.game_map.set_exit(prev, direction, current_room.name)
                # Auto-record the reverse exit so BFS can place rooms correctly
                opp = self.OPPOSITE_DIR.get(direction)
                if opp and opp not in current_room.exits:
                    self.game_map.set_exit(current_room.name, opp, prev)
            # Always update here on movement so highlight follows player (even brief mode / same room)
            self.game_map.here = current_room.name
        else:
            # No direction = explicit look/sync; always update here
            self.game_map.here = room_name

        self._save_map()

    def kill(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

# ---------- SESSION REGISTRY ----------
_sessions: dict[tuple[int, int], GameSession] = {}
_channel_game: dict[tuple[int, int], str] = {}  # (guild, channel) -> game stem name

DEFAULT_GAME = "planetfall"

def get_session(guild_id: int, channel_id: int) -> "GameSession | None":
    key = (guild_id, channel_id)
    if key not in _sessions:
        games = list_games()
        game_name = _channel_game.get(key, DEFAULT_GAME)
        game_path = games.get(game_name)
        if game_path is None:
            if not games:
                return None
            game_path = next(iter(games.values()))
        _sessions[key] = GameSession(guild_id, channel_id, game_path)
    return _sessions[key]

# ---------- DIRECTION ALIASES ----------
DIRECTION_ALIASES = {
    "n": "north", "s": "south", "e": "east", "w": "west",
    "u": "up", "d": "down",
    "ne": "northeast", "nw": "northwest",
    "se": "southeast", "sw": "southwest",
    "north": "north", "south": "south", "east": "east", "west": "west",
    "up": "up", "down": "down",
    "northeast": "northeast", "northwest": "northwest",
    "southeast": "southeast", "southwest": "southwest",
}

# ---------- DISCORD EVENTS ----------
@CLIENT.event
async def on_ready():
    print(f"Logged in as {CLIENT.user} (ID: {CLIENT.user.id})")
    print("Watching for '>' commands...")

@CLIENT.event
async def on_message(message: discord.Message):
    if message.author == CLIENT.user:
        return
    if not isinstance(message.channel, discord.TextChannel):
        return

    content = message.content.strip()
    guild_id = message.guild.id if message.guild else 0
    channel_id = message.channel.id

    if not content.startswith(">"):
        return  # ignore non-game messages silently

    raw_cmd = content[1:].strip().lower()

    # >games – list available games
    if raw_cmd == "games":
        games = list_games()
        if not games:
            await message.channel.send("No games found in ~/zmux-games/.")
            return
        key = (guild_id, channel_id)
        current = _channel_game.get(key, DEFAULT_GAME)
        lines = ["Available games (use >game <name> to switch):"]
        for name in games:
            marker = " <-- current" if name == current else ""
            lines.append(f"  {name}{marker}")
        await message.channel.send("```\n" + "\n".join(lines) + "\n```")
        return

    # >game <name> – switch to a different game
    if raw_cmd.startswith("game "):
        game_name = raw_cmd[5:].strip().lower()
        games = list_games()
        if game_name not in games:
            names = ", ".join(games.keys()) or "none"
            await message.channel.send(f"Unknown game '{game_name}'. Available: {names}")
            return
        key = (guild_id, channel_id)
        if key in _sessions:
            _sessions[key].kill()
            del _sessions[key]
        _channel_game[key] = game_name
        session = get_session(guild_id, channel_id)
        if session is None:
            await message.channel.send("No games available.")
            return
        session.game_map = GameMap()
        await session.start(restore=True)
        await message.channel.send(f"```\nSwitched to {game_name}. Restored from save if one exists.\n```")
        return

    # >maptemp <query> – mark rooms matching query as temp-named (will be corrected on next visit)
    if raw_cmd.startswith("maptemp "):
        query = raw_cmd[8:].strip()
        session = get_session(guild_id, channel_id)
        if session is None:
            await message.channel.send("No games available in ~/zmux-games/.")
            return
        renamed = session.game_map.mark_temp(query)
        if renamed:
            session._save_map()
            lines = "\n".join(renamed)
            await message.channel.send(f"```\nTemp-renamed:\n{lines}\n(will auto-correct on next visit)\n```")
        else:
            await message.channel.send(f"```\nNo rooms matching '{query}' found.\n```")
        return

    # >mapclear – wipe the entire map and start fresh
    if raw_cmd == "mapclear":
        session = get_session(guild_id, channel_id)
        if session is None:
            await message.channel.send("No games available in ~/zmux-games/.")
            return
        session.game_map = GameMap()
        session._save_map()
        await message.channel.send("```\nMap cleared.\n```")
        return

    # >mapdel <query> – delete rooms matching query from the map
    if raw_cmd.startswith("mapdel "):
        query = raw_cmd[7:].strip()
        session = get_session(guild_id, channel_id)
        if session is None:
            await message.channel.send("No games available in ~/zmux-games/.")
            return
        deleted = session.game_map.delete_room(query)
        if deleted:
            session._save_map()
            await message.channel.send(f"```\nRemoved from map: {', '.join(deleted)}\n```")
        else:
            await message.channel.send(f"```\nNo rooms matching '{query}' found.\n```")
        return

    # >map / >showmap
    if raw_cmd in ("map", "showmap"):
        session = get_session(guild_id, channel_id)
        if session is None:
            await message.channel.send("No games available in ~/zmux-games/.")
            return
        img_bytes = session.game_map.to_image()
        if img_bytes is None:
            await message.channel.send("```\n(no map data yet)\n```")
            return
        await message.channel.send(file=discord.File(io.BytesIO(img_bytes), filename="map.png"))
        return

    # >new  – start a completely fresh game
    if raw_cmd == "new":
        key = (guild_id, channel_id)
        if key in _sessions:
            _sessions[key].kill()
            del _sessions[key]
        session = get_session(guild_id, channel_id)
        if session is None:
            await message.channel.send("No games available in ~/zmux-games/.")
            return
        session.game_map = GameMap()
        await session.start(restore=False)
        await message.channel.send("```\nNew game started.\n```")
        return

    # >load – restart and restore from quicksave
    if raw_cmd == "load":
        key = (guild_id, channel_id)
        if key in _sessions:
            _sessions[key].kill()
            del _sessions[key]
        session = get_session(guild_id, channel_id)
        if session is None:
            await message.channel.send("No games available in ~/zmux-games/.")
            return
        await session.start(restore=True)
        await message.channel.send("```\nGame loaded from quicksave.\n```")
        return

    # >load <slot> – load from a named save slot
    if raw_cmd.startswith("load "):
        slot = raw_cmd[5:].strip()
        session = get_session(guild_id, channel_id)
        if session is None:
            await message.channel.send("No games available in ~/zmux-games/.")
            return
        result = await session.load_from_slot(slot)
        if len(result) > 1900:
            result = result[:1900] + "\n...[truncated]..."
        await message.channel.send(f"```\n{result}\n```")
        return

    # >save <slot> – save to a named slot
    if raw_cmd.startswith("save "):
        slot = raw_cmd[5:].strip()
        session = get_session(guild_id, channel_id)
        if session is None:
            await message.channel.send("No games available in ~/zmux-games/.")
            return
        result = await session.save_to_slot(slot)
        await message.channel.send(f"```\n{result}\n```")
        return

    # >saves – list all save slots for current game
    if raw_cmd == "saves":
        session = get_session(guild_id, channel_id)
        if session is None:
            await message.channel.send("No games available in ~/zmux-games/.")
            return
        slots = session.list_saves()
        if not slots:
            await message.channel.send("```\nNo saves found for this game.\n```")
        else:
            lines = ["Save slots for " + session.game_name + ":"] + [f"  {s}" for s in slots]
            await message.channel.send("```\n" + "\n".join(lines) + "\n```")
        return

    # General game command
    if not raw_cmd:
        await message.channel.send("Please provide a command after '>'.")
        return

    session = get_session(guild_id, channel_id)
    if session is None:
        await message.channel.send("No games available in ~/zmux-games/. Add a .z* file there first.")
        return

    # Ensure process is running
    if session.proc is None or session.proc.returncode is not None:
        await session.start(restore=True)

    # Figure out if this is a movement command
    direction = DIRECTION_ALIASES.get(raw_cmd)

    # Actually use the original casing for the command sent to dfrotz
    raw_cmd_orig = content[1:].strip()
    if raw_cmd == "x" or raw_cmd.startswith("x "): raw_cmd_orig = "examine" + raw_cmd_orig[1:]

    try:
        game_out = await session.send_command(raw_cmd_orig)
    except Exception as e:
        await message.channel.send(f"```\nError communicating with game: {e}\n```")
        session.kill()
        return

    # Update map — only on movement commands or explicit look
    if direction or raw_cmd == "look":
        session.update_map(game_out, direction)

    # Detect death (game offers Restart/Restore/Quit)
    upper_out = game_out.upper()
    if "RESTART" in upper_out and "RESTORE" in upper_out and ("QUIT" in upper_out or "FULL" in upper_out):
        slots = session.list_saves()
        slot_lines = "\n".join(
            f"  >load  (quicksave)" if s == "(quicksave)" else f"  >load {s}" for s in slots
        ) if slots else "  (no saves found — use >new to start over)"
        death_msg = game_out.strip() + "\n\n💀 You died! Use >load or pick a save:\n" + slot_lines
        # Dismiss the game's own prompt so dfrotz doesn't hang
        await session._send("quit")
        await session._read_until_prompt(timeout=2.0)
        session.kill()
        if len(death_msg) > 1900:
            death_msg = death_msg[:1900] + "\n...[truncated]..."
        await message.channel.send(f"```\n{death_msg}\n```")
        return

    # Truncate to stay under Discord's 2000-char limit
    if len(game_out) > 1900:
        game_out = game_out[:1900] + "\n...[truncated]..."

    await message.channel.send(f"```\n{game_out}\n```")

@CLIENT.event
async def on_disconnect():
    for session in _sessions.values():
        session.kill()

# ---------- RUN ----------
if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
    if not TOKEN:
        print("ERROR: Set DISCORD_BOT_TOKEN environment variable.", file=sys.stderr)
        sys.exit(1)
    CLIENT.run(TOKEN)
