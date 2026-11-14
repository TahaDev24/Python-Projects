# FILE ORGANIZER

**A preview-first file organization utility built with Python and Kivy.**

FILE ORGANIZER scans the files directly inside a selected folder, groups them by file type, and prepares a move plan for the user to review before any files are moved.

## Features

- Select a folder through the graphical file chooser.
- Classify files by extension.
- Preview the proposed organization before execution.
- Move files only after user confirmation.
- Avoid overwriting existing files by generating an alternative destination name.
- Display a summary of completed moves and errors.
- Work locally without requiring an external service.

## File categories

| Category | Example extensions |
| --- | --- |
| Images | `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.heic` |
| Videos | `.mp4`, `.mkv`, `.mov`, `.avi`, `.webm` |
| Audio | `.mp3`, `.wav`, `.aac`, `.ogg`, `.flac` |
| Documents | `.pdf`, `.docx`, `.txt`, `.xlsx`, `.pptx` |
| Archives | `.zip`, `.rar`, `.7z`, `.tar`, `.gz` |
| Source code | `.py`, `.js`, `.html`, `.css`, `.java`, `.json`, `.sql` |
| Installers | `.apk`, `.exe`, `.msi`, `.dmg`, `.deb` |
| Other | Extensions not listed above |

## Requirements

- Python 3
- Kivy

Install Kivy in a compatible Python environment:

```bash
python -m pip install kivy
```

Optional Persian text shaping:

```bash
python -m pip install arabic-reshaper python-bidi
```

## Run on desktop

From this directory:

```bash
python file_organizer.py
```

## Run on Android with Pydroid 3

1. Make sure Kivy is installed and working in Pydroid 3.
2. Grant the storage permissions required by your Android version.
3. Open and run `file_organizer.py`.
4. Select a folder, review the preview, and confirm only when the proposed moves are correct.

Android may restrict access to protected storage locations even when application permissions are enabled.

## Behavior and limitations

- Only files directly inside the selected folder are considered; subdirectories are not scanned recursively.
- Files are moved into category folders; they are not intentionally deleted.
- A move plan must be reviewed and confirmed before execution.
- Existing destination files are not overwritten; a unique alternative name is generated.
- An automatic undo feature is not documented in the current implementation.

> [!CAUTION]
> Test the organizer on a sample folder first. Keep a backup of important files before organizing them.

## Project files

```text
FILE-ORGANIZER/
├── file_organizer.py
└── README.md
```

[Back to repository overview](../README.md)
