# Hermes Z-Machine Discord Bot

A Discord bot that drives [dfrotz](https://gitlab.com/DavidGriffith/frotz) to play Infocom-style Z-machine interactive fiction games in Discord channels.

## Features

- Spawns a dfrotz process per (guild, channel) via async subprocess
- Accepts game commands prefixed with `>` (e.g. `>look`, `>n`, `>save`)
- Auto-generates an ASCII/Pillow PNG map of explored rooms with three-dimensional floor layers
- Named save slots (`>save slotname`, `>load slotname`)
- Multi-game support — drop `.z3`–`.z8` files into `~/zmux-games/`, switch with `>game <name>`
- Map editing: `>mapdel <query>`, `>maptemp <query>` (auto-correct on revisit)
- Progress persists across bot restarts

## Quick Start

1. Install dfrotz: `sudo apt install frotz`
2. Install Python deps: `pip install discord.py Pillow`
3. Set `DISCORD_BOT_TOKEN` in your environment
4. Drop Z-machine game files into `~/zmux-games/`
5. Run: `python3 zmux_bot.py`

## Commands

| Command | Description |
|---------|-------------|
| `>look` | Look around / start game |
| `>n`, `>s`, `>e`, `>w`, `>u`, `>d` | Move |
| `>save [slot]` | Save game (optional named slot) |
| `>load [slot]` | Load game (quicksave or named slot) |
| `>saves` | List named save slots |
| `>new` | Start fresh game |
| `>game <name>` | Switch to a different game |
| `>games` | List available games |
| `>map` | Show PNG map of explored rooms |
| `>mapdel <query>` | Delete matching rooms from map |
| `>maptemp <query>` | Rename junk rooms temporarily (auto-corrects on revisit) |

## File Layout

- `~/zmux-games/` — game files (.z3, .z5, .z8, .zblorb)
- `~/.hermes/zmux-saves/` — save files and map data (per guild, channel, game)

## Compatible Games

Any standard Z-machine game (Infocom, modern IF). Tested with Planetfall.
