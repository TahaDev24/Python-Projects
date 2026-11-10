🏛️ JUMONG BOT

An interactive Python bot for the Bale messaging platform, inspired by the historical world of Jumong and Goguryeo. The project brings together a collection of independent games, branching story scenarios, scoring systems, and informational content in one integrated structure. Users interact with the bot through dynamic menus, step-by-step choices, and dedicated events.

✨ Main Features

- 🏹 Four interactive experiences: archery, war story, knowledge quiz, and personality quiz
- ⚔️ Branching narrative system with multiple decision paths
- 🏯 Multi-section content structure inspired by the history and world of Jumong
- 🎮 User-choice-based stages and interactive scenarios
- 🏆 User scoring and progress tracking
- 🔐 Hidden sections and special paths that can be unlocked
- 🧭 Main menu with 8 separate sections and unified navigation
- 📜 Historical content, characters, locations, and story sections
- 🔘 Management of pages, buttons, and interactive events

🧰 Technical Structure

- 🐍 Built with Python 3.9+
- 🧩 Single-file architecture with an integrated processing structure
- 🔌 Direct communication with the Bale Bot API
- 🔄 Event retrieval and processing through Long Polling
- 💾 User scores stored in JSON format
- ⚙️ Game-state and interactive-flow management
- 🛠️ Network error handling, retries, and API responses
- 🔒 Input validation and callback handling
- 📦 No external libraries required

🚀 Installation and Setup

1. Make sure Python 3.9 or later is installed on your server.
2. Get the `jumong_bot.py` file.
3. Set the bot token in the file's configuration section.
4. Run the bot:

```bash
python3 jumong_bot.py
```

To run the internal tests:

```bash
python3 jumong_bot.py --selftest
```

🛡️ Management and Security

- Validation of incoming messages and events
- Handling and processing of Callback Queries
- Handling connection errors and invalid API responses
- Retry system for failed requests
- Request rate-limit management
- Protection against interruptions in game execution flows
- Handling incomplete states and cleaning up temporary data
- User score storage based on numeric identifiers

📌 Project Status

The main parts of the project have been implemented. The code structure has been checked for syntax, and 48 internal tests have been run successfully. These tests cover navigation, games, quizzes, story paths, score storage, event processing, and error handling. In addition, 112 navigation paths and buttons were checked during the test process.