# Cycling Race Timer

Local race timing and ranking application for circuit cycling events.

The application is designed for an official at the finish line. It runs locally
with FastAPI and SQLite, with no remote server or cloud service required.

## Features

- Manage multiple independent races.
- Configure a race by lap count or target duration.
- Optionally define the lap distance.
- Add bib numbers individually, by space-separated list, or by range
  (`1 2 3` or `4-65`).
- Exclude individual bibs or bib ranges.
- Start races independently or start several selected races together.
- Display live timers with centisecond precision (`mm:ss.cc`).
- Record and review every passage.
- Show the race associated with each recent passage.
- Edit race names and bib assignments.
- Edit race characteristics, including lap count, target duration, and lap
  distance.
- Scroll rankings containing more than ten riders.
- Import participants from UTF-8 CSV or XLSX files.
- Persist data in a local SQLite database.

## Requirements

- Python 3.10 or newer
- Windows, macOS, or Linux

## Installation

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Build a Windows application

The repository includes a PyInstaller configuration for distributing the
application to Windows users who do not have Python installed. From a
PowerShell terminal:

```powershell
.\build_windows.ps1
```

The portable application is created as
`dist\ChronometrageCycliste.exe`.
The application opens the local web interface automatically and stores its
SQLite database in a `data` folder next to the executable.

## Run the application

Start the local server:

```powershell
python run.py
```

Open <http://127.0.0.1:8000> in a browser.

Press `Ctrl+C` in the terminal to stop the server.

## Race setup

1. Create a race with a unique name.
2. Choose either a fixed number of laps or a target duration.
3. Optionally enter the distance of one lap.
4. Click **Next** and enter bibs separated by spaces or using ranges.
5. Enter exclusions using the same format.
6. Start the race individually, or select several ready races and start them
   together.

Race characteristics, including the name, can be edited later with the
**Modify** button. If no lap distance is configured, the average-speed column
is hidden from the ranking.

Bib setup is cumulative: each click on **Save bibs** adds a new batch of bibs
and exclusions without removing previous entries.

For a target-duration race, the first two recorded passages are used to
estimate the average lap time. The application then calculates the nearest
whole number of laps for the target duration. Corrections or cancellations of
passages recalculate this value.

A bib with recorded passages cannot be removed from a race, preserving the
timing history.

## Timing and rankings

The timer is rendered live in the browser and displayed to centiseconds. The
server periodically resynchronizes the displayed value.

During a race, riders are ranked by:

1. Number of completed laps, descending.
2. Last passage time, ascending, when riders have completed the same number of
   laps.

Rankings with more than ten riders use an internal scroll area to keep the
race dashboard compact.

## Tests

Run the test suite with:

```powershell
python -m pytest
```

The test suite covers the main race, timing, passage, ranking, validation,
and frontend rendering behaviors.

## Project structure

```text
.
├── app/
│   ├── __init__.py
│   └── main.py          # FastAPI application and domain logic
├── data/                 # Local runtime data, ignored by Git
├── tests/
│   └── test_app.py       # Automated tests
├── build_windows.ps1     # Windows packaging script
├── chronometrage.spec    # PyInstaller configuration
├── .gitignore
├── requirements.txt
├── run.py                # Local application entry point
└── README.md
```

The local SQLite database is created as `data/cycling_timer.sqlite3` and is
intentionally ignored by Git.