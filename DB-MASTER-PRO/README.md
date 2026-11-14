# DB MASTER PRO

**A single-file SQLite administration studio built with Python and Kivy.**

DB MASTER PRO provides a graphical workspace for inspecting and managing SQLite database files. The application is designed for desktop-style use and Android environments that support Kivy, including Pydroid 3.

## Features

- Open and inspect SQLite database files.
- Browse tables, columns, and records.
- Execute SQL statements.
- Add and edit records.
- Create and manage indexes.
- Back up and restore database files.
- Import and export tabular data using CSV.
- Run database maintenance operations such as integrity checks, `ANALYZE`, and `VACUUM`.

## Technology

| Component | Purpose |
| --- | --- |
| Python | Application logic |
| Kivy | Graphical user interface |
| SQLite | Database engine |
| CSV | Data exchange |

## Requirements

- Python 3
- Kivy
- Optional: `arabic-reshaper` and `python-bidi` for improved Persian text rendering in environments that need text shaping.

Install the dependencies:

```bash
python -m pip install kivy
```

Optional Persian text support:

```bash
python -m pip install arabic-reshaper python-bidi
```

## Run

From this directory, execute:

```bash
python DB_MASTER_PRO.py
```

## Android / Pydroid 3

1. Install and configure Kivy in Pydroid 3.
2. Grant the file access permissions required by your Android version.
3. Open `DB_MASTER_PRO.py` in Pydroid 3 and run it.
4. Select a database file that you are authorized to access.

Android storage restrictions may prevent access to some protected directories. Availability depends on the Android version and device configuration.

## Data safety

> [!WARNING]
> SQL statements and maintenance operations can modify or remove database content. Back up important databases before running write queries, restoring backups, or performing maintenance.

Do not commit personal database files or production data to this public repository.

## Project files

```text
DB-MASTER-PRO/
├── DB_MASTER_PRO.py
└── README.md
```

[Back to repository overview](../README.md)
