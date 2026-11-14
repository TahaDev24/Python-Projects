# SAFABAT

**A multi-purpose companion bot for groups on Bale Messenger.**

SAFABAT combines group utilities, entertainment, games, progression systems, and moderation features in a single Python application backed by SQLite.

## Features

- Group menus and configurable settings.
- Text-triggered responses and entertainment tools.
- Jokes, joke ratings, and fortune features.
- A collection of interactive games.
- XP, levels, medals, missions, and league progression.
- User and group statistics.
- Group management and moderation tools.
- Events and experimental features.
- Persistent storage using SQLite.
- Application logging and single-instance locking.

The feature list describes the capabilities represented in the source code. Availability in a live group depends on bot permissions and the Bale platform.

## Requirements

- Python 3.10 or later
- `requests`
- A Bale bot token

Install the dependency:

```bash
python -m pip install requests
```

## Configuration

SAFABAT reads credentials from environment variables.

| Variable | Required | Description |
| --- | --- | --- |
| `BALE_BOT_TOKEN` | Yes | Token for the Bale bot |
| `BALE_ADMIN_ID` | No | Numeric ID of the primary administrator; defaults to `0` |

Example for Linux and macOS:

```bash
export BALE_BOT_TOKEN="YOUR_BOT_TOKEN"
export BALE_ADMIN_ID="YOUR_NUMERIC_ADMIN_ID"
```

PowerShell example:

```powershell
$env:BALE_BOT_TOKEN = "YOUR_BOT_TOKEN"
$env:BALE_ADMIN_ID = "YOUR_NUMERIC_ADMIN_ID"
```

## Run

From this directory:

```bash
python safabat_bot.py
```

The bot requires an internet connection to communicate with Bale. On startup, it creates its SQLite database and runtime files alongside the source file.

## Built-in self-test

Send the following command in the bot's supported test context:

```text
/selftest
```

The self-test uses a temporary database. It does not replace testing the bot in a dedicated test group.

## Runtime files

| File | Purpose |
| --- | --- |
| `safabat.db` | SQLite database |
| `safabat.log` | Runtime log |
| `safabat.lock` | Single-instance lock |

These files may contain user or group activity data. Keep them out of public commits and restrict access on the host.

## Deployment notes

1. Configure the environment variables before starting the process.
2. Add the bot to a test group first.
3. Grant administrator permissions only if the moderation features you intend to use require them.
4. Verify the bot's behavior and permissions before adding it to production groups.

> [!WARNING]
> Never hard-code a real token into a public source file. If a token is exposed, revoke it immediately and replace it.

## Project files

```text
SAFA-MANAGEMENT/
├── safabat_bot.py
└── README.md
```

[Back to repository overview](../README.md)
