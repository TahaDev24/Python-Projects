🤖 SAFABAT

Safabat is a multi-purpose, single-file bot for Bale messaging groups, combining entertainment, competition, and group management features.

Security note: The bot token and administrator ID are confidential. Do not place them in source code or public GitHub files.

✨ Features

- Group menus and settings
- Text-based responses
- Jokes, joke meter, and fortune feature
- 13 entertaining games
- Experience points (XP), levels, medals, missions, and league system
- Statistics and bot memory/DNA features
- Group management tools
- Labs and events
- SQLite data storage
- Runtime and error logging

This feature list is based on the project description and code structure. Not all features have been fully verified in the live Bale environment.

🧰 Requirements

- Python 3.10 or later
- `requests` library
- A valid Bale bot token

🚀 Installation and Setup

1. Install the required library:

```bash
python -m pip install requests
```

If the `python` command is not available in your environment, use `python3`.

2. Configure the bot credentials.

The application reads sensitive values from environment variables:

| Variable | Purpose | Status |
|---|---|---|
| `BALE_BOT_TOKEN` | Bale bot token | Required |
| `BALE_ADMIN_ID` | Numeric ID of the main administrator | Optional; defaults to 0 |

On Linux or terminals that support `export`, set the variables before running the application:

```bash
export BALE_BOT_TOKEN="your_new_bot_token"
export BALE_ADMIN_ID="numeric_admin_id"
python safabat_bot.py
```

The method for setting environment variables may differ in Pydroid, Windows, and hosting services.

Important: Never put a real token in the README, source code, public messages, or a GitHub repository. If a token has already been exposed, revoke it through the Bale panel and use a new one.

3. Run the bot:

```bash
python safabat_bot.py
```

An internet connection is required to communicate with the Bale API. The `safabat.db` database is created next to the application file. The program also uses log and lock files.

4. Run the internal test.

After setup, the following command is available for internal tests:

```text
/selftest
```

Internal tests are not a substitute for checking every feature in a test Bale group.

👥 Adding the Bot to a Group

- Add the bot to the desired Bale group.
- Grant only the permissions it needs. Features such as deleting messages or banning members may require administrator permissions.
- Before regular use, test the bot's features and permission level in a test group.

📁 Important Files

| File | Description |
|---|---|
| `safabat_bot.py` | Main bot source code |
| `safabat.db` | SQLite database |
| `safabat.log` | Runtime logs |
| `safabat.lock` | File used to prevent concurrent execution |

Files created at runtime are not necessarily included in the GitHub repository.

🔐 Security Notes

- Do not publish the bot token or private information in a public repository.
- If a token is exposed, revoke it and generate a new one.
- The database and logs may contain group activity information; do not publish them publicly.
- Test the bot in a controlled environment before granting it administrator access.

📌 Project Status

Safabat is under development. Connection stability, runtime compatibility, and the behavior of all features still need to be checked in practice. These documents alone do not mean that the bot's complete functionality has been verified.