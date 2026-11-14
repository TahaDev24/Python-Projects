# WORLD WAR 4

**A turn-based strategy and nation-management game delivered through Bale Messenger.**

WORLD WAR 4 combines economic development, research, trade, diplomacy, alliances, and turn-based battles in a persistent multiplayer game.

## Game systems

- World map containing six regions, 20 countries, and approximately 130 cities.
- Economy with nine resources, production, consumption, taxation, and inflation.
- Buildings and upgrade paths.
- Research and technology progression.
- Military units and unit categories.
- Turn-based battles and city-control mechanics.
- Markets, trade orders, and investments.
- Alliances, shared treasuries, and voting.
- Diplomacy, treaties, and world events.
- Missions, achievements, and leaderboards.

## Technical overview

| Component | Details |
| --- | --- |
| Language | Python 3.9+ |
| Platform | Bale Messenger |
| Storage | SQLite |
| API client | Python standard library |
| Architecture | Single-file application with separated internal game systems |
| Tests | Built-in self-test mode |

The application uses SQLite transactions and a local database file. It also includes backup-related functionality and request-handling safeguards.

## Requirements

- Python 3.9 or later
- A Bale bot token
- A host with outbound internet access and permission to keep a Python process running

The source file uses the Python standard library and does not require third-party packages.

## Configuration

Before deployment, open `world_war_4.py` and replace the placeholder value assigned to `BALE_BOT_TOKEN` with your bot token. Configure `ADMIN_ID` with the numeric administrator ID.

Keep real credentials local. Do not commit a configured copy of the source file or share your token in issue reports.

## Run

Start the game bot:

```bash
python3 world_war_4.py
```

Run the built-in test suite:

```bash
python3 world_war_4.py --selftest
```

## Data and backups

- The main database is created as `world_war_4.db` beside the source file.
- Backup files are stored in the `ww4_backups/` directory.
- Database and backup files may contain persistent player data. Do not publish them.

> [!WARNING]
> Back up the database before upgrading or changing the application. Test migrations and restore procedures on a copy before using a production database.

## Deployment checklist

- [ ] Configure the bot token and administrator ID.
- [ ] Run the built-in self-test.
- [ ] Start the bot in a test environment.
- [ ] Verify bot permissions and core game flows.
- [ ] Confirm that database backups are being created and can be restored.
- [ ] Keep the host, database, and credentials access-controlled.

## Project files

```text
WORLD-WAR-4/
├── world_war_4.py
└── README.md
```

[Back to repository overview](../README.md)
