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
import json
import os
import re
import shutil
import sys
from collections import deque
from pathlib import Path

import discord

# ---------- CONFIG ----------
INTENTS = discord.Intents.default()
INTENTS.message_content = True
CLIENT = discord.Client(intents=INTENTS)

BASE_DIR = Path.home() / ".hermes" / "zmux-saves"
BASE_DIR.mkdir(parents=True, exist_ok=True)

GAMES_DIR = Path.home() / "zmux-games"
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
                self.here = self.start
            if self.start == name:
                self.start = next(iter(self.rooms), None)
        if targets:
            pass  # caller is responsible for saving the map
        return targets

    def to_ascii(self) -> str:
        if not self.start or self.start not in self.rooms:
            return "(no map data yet)"

        # Direction -> (dx, dy, dz): up/down get their own z-layer
        DIR_DELTA: dict[str, tuple[int, int, int]] = {
            "north": (0, -1, 0), "south": (0,  1, 0),
            "west":  (-1, 0, 0), "east":  (1,  0, 0),
            "up":    (0,  0,  1), "down":  (0,  0, -1),
            "northeast": (1, -1, 0), "northwest": (-1, -1, 0),
            "southeast": (1,  1, 0), "southwest": (-1,  1, 0),
        }
        DIR_CONN: dict[tuple[int, int], str] = {
            (0, -1): " │ ", (0,  1): " │ ",
            (1, -1): " ╱ ", (-1, -1): " ╲ ",
            (1,  1): " ╲ ", (-1,  1): " ╱ ",
        }

        # BFS tracking (x, y, z)
        pos3: dict[str, tuple[int, int, int]] = {self.start: (0, 0, 0)}
        q = deque([self.start])
        while q:
            cur = q.popleft()
            cx, cy, cz = pos3[cur]
            for d, dst in self.rooms[cur].exits.items():
                if dst not in pos3:
                    dx, dy, dz = DIR_DELTA.get(d, (0, 0, 0))
                    pos3[dst] = (cx + dx, cy + dy, cz + dz)
                    q.append(dst)

        # Group rooms by floor (z)
        floors: dict[int, dict[str, tuple[int, int]]] = {}
        for name, (x, y, z) in pos3.items():
            floors.setdefault(z, {})[name] = (x, y)

        CELL_W = 11
        EMPTY = " " * CELL_W
        layer_chunks: list[str] = []

        for z in sorted(floors.keys(), reverse=True):
            layer_pos = floors[z]
            xs = [p[0] for p in layer_pos.values()]
            ys = [p[1] for p in layer_pos.values()]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)

            gcols = (max_x - min_x) * 2 + 1
            grows = (max_y - min_y) * 2 + 1
            grid = [[EMPTY] * gcols for _ in range(grows)]

            # Place rooms
            for name, (x, y) in layer_pos.items():
                gx = (x - min_x) * 2
                gy = (y - min_y) * 2
                if self.here == name:
                    inner = name[: CELL_W - 2]
                    label = f"[{inner}]"
                else:
                    label = name[: CELL_W]
                grid[gy][gx] = label.center(CELL_W)

            # Place same-floor connectors
            seen_conns: set[tuple[str, str]] = set()
            for name, (x, y) in layer_pos.items():
                for d, dst in self.rooms[name].exits.items():
                    if dst not in layer_pos:
                        continue
                    pair = tuple(sorted([name, dst]))
                    if pair in seen_conns:
                        continue
                    seen_conns.add(pair)  # type: ignore[arg-type]

                    dx, dy, dz = DIR_DELTA.get(d, (0, 0, 0))
                    if dz != 0:
                        continue
                    if layer_pos.get(dst) != (x + dx, y + dy):
                        continue  # layout conflict

                    cgx = (x - min_x) * 2 + dx
                    cgy = (y - min_y) * 2 + dy
                    if not (0 <= cgx < gcols and 0 <= cgy < grows):
                        continue

                    if dy == 0 and dx != 0:
                        conn = "─" * CELL_W
                    else:
                        conn = DIR_CONN.get((dx, dy), "   ")
                    grid[cgy][cgx] = conn.center(CELL_W)

            lines = ["".join(row).rstrip() for row in grid]
            floor_label = f"Floor {z:+d}" if z != 0 else "Ground"
            header = f"── {floor_label} " + "─" * max(0, 30 - len(floor_label))
            layer_chunks.append(header + "\n" + "\n".join(lines))

        return "\n\n".join(layer_chunks)

    def to_dict(self):
        return {
            "start": self.start,
            "here": self.here,
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

        if restore and self.save_path.is_file():
            await self._send("restore")
            await self._read_until_prompt(timeout=2.0)  # filename prompt
            await self._send(str(self.save_path))
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
        )
        first_lower = lines[0].lower()
        if direction and any(p in first_lower for p in FAILED_MOVE_PHRASES):
            return

        room_name = lines[0]
        description = ""
        for ln in lines[1:]:
            if ln and not ln.startswith(">"):
                description = ln[:80]
                break

        current_room = self.game_map.add_room(room_name, description)

        if direction and self.game_map.here:
            if current_room.name != self.game_map.here:
                self.game_map.set_exit(self.game_map.here, direction, current_room.name)
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
        await message.channel.send(f"```\n{session.game_map.to_ascii()}\n```")
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

    # Update map
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
