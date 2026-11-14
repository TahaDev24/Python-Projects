# Contributing

Thank you for considering a contribution to this repository.

The projects in this repository are maintained as independent applications. Keep changes focused, document project-specific behavior, and avoid introducing changes that affect unrelated projects.

## Before you start

1. Check the existing [issues](https://github.com/TahaDev24/Python-Projects/issues) and pull requests.
2. Read the README for the project you intend to change.
3. For a significant change, open an issue first to discuss the proposed approach.

## Development workflow

1. Create a branch from `main`.
2. Make a focused change in the relevant project directory.
3. Keep naming and formatting consistent with the existing source.
4. Update the project's README when setup, behavior, configuration, or limitations change.
5. Run the checks relevant to the project.
6. Open a pull request with a clear summary and verification notes.

Example branch names:

- `docs/update-file-organizer-guide`
- `fix/safabat-command-handler`
- `feature/db-export-options`

## Testing

Use the checks provided by the project you changed.

| Project | Available check |
| --- | --- |
| JUMONG BOT | `python3 jumong_bot.py --selftest` |
| SAFABAT | `/selftest` in the supported bot test context |
| WORLD WAR 4 | `python3 world_war_4.py --selftest` |
| DB MASTER PRO | Manual UI and database-operation checks |
| FILE ORGANIZER | Manual preview and file-move checks on a test folder |

Do not claim a test passed unless you actually ran it. If a check cannot be run, state that clearly in the pull request.

## Pull request guidelines

Include:

- A concise description of the change.
- The reason for the change.
- The affected project and files.
- Commands or manual steps used for verification.
- Screenshots for relevant interface changes, when useful.

Keep pull requests small enough to review. Do not combine unrelated refactors across multiple projects.

## Security and privacy

- Never commit bot tokens, passwords, API keys, or private identifiers.
- Do not attach real user databases, logs, or private group data.
- Use test accounts and sample data when reproducing issues.
- Report suspected vulnerabilities according to [SECURITY.md](SECURITY.md).

## Code of conduct

Be respectful, constructive, and considerate when discussing issues and reviewing contributions.
