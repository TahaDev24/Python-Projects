# JUMONG BOT

**An interactive, story-driven bot inspired by the world of Jumong and Goguryeo.**

JUMONG BOT brings short games, branching story experiences, quizzes, and themed content to Bale Messenger through an interactive bot interface.

## Features

- Four interactive experiences: archery, war-themed storytelling, general knowledge quizzes, and a personality quiz.
- Branching story paths shaped by user choices.
- Character, location, and story-related information.
- Progress and game-score tracking.
- Unlockable hidden sections and special paths.
- Menu-driven navigation and callback interactions.
- Optional welcome image support; the bot can operate in text-only mode.

## Technical overview

| Component | Details |
| --- | --- |
| Language | Python 3.9+ |
| Platform | Bale Messenger |
| Bot interface | Bale Bot API |
| Update handling | Long polling |
| Score storage | JSON file |
| Dependencies | Python standard library |

The bot uses a single Python source file and a lightweight API client implemented with the standard library.

## Setup

1. Install Python 3.9 or later.
2. Download or clone this repository.
3. Open `jumong_bot.py`.
4. Set the bot token in the `BOT_TOKEN` configuration constant.
5. Keep the real token out of version control.
6. Run the bot:

```bash
python3 jumong_bot.py
```

## Self-test

Run the built-in checks with:

```bash
python3 jumong_bot.py --selftest
```

## Configuration and data

- The bot token is configured through `BOT_TOKEN` in the source file.
- Optional welcome artwork can be configured in the `IMAGES` mapping.
- Scores are stored at the path provided by `JUMONG_SCORES_FILE`.
- If `JUMONG_SCORES_FILE` is not set, the bot uses `jumong_scores.json` next to the source file.
- If the runtime cannot write to the selected location, scores remain in memory only until the process stops.

> [!WARNING]
> Do not publish your bot token in source code, commits, screenshots, or issue reports. If a token is exposed, revoke it and create a replacement through the platform.

## Project files

```text
JUMONG-BOT/
├── jumong_bot.py
└── README.md
```

[Back to repository overview](../README.md)
