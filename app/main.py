from __future__ import annotations

import csv
import io
import sqlite3
import sys
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = (
    Path(sys.executable).resolve().parent / "data"
    if getattr(sys, "frozen", False)
    else BASE_DIR / "data"
)
DATA_DIR.mkdir(exist_ok=True)
DATABASE = DATA_DIR / "cycling_timer.sqlite3"

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Cycling Race Timer", lifespan=lifespan)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    with closing(connect()) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS races (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                laps INTEGER NOT NULL CHECK (laps > 0),
                target_seconds REAL,
                lap_distance_km REAL NOT NULL CHECK (lap_distance_km > 0),
                distance_defined INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'ready'
                    CHECK (status IN ('ready', 'running', 'paused', 'stopped', 'finished')),
                started_at TEXT,
                paused_at TEXT,
                paused_seconds REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
                bib TEXT NOT NULL,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                UNIQUE(race_id, bib)
            );
            CREATE TABLE IF NOT EXISTS passages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
                participant_id INTEGER NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
                recorded_at TEXT NOT NULL,
                elapsed_seconds REAL NOT NULL,
                lap_number INTEGER NOT NULL,
                lap_seconds REAL NOT NULL,
                cancelled INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_passages_participant
                ON passages(participant_id, cancelled, recorded_at);
            CREATE TABLE IF NOT EXISTS race_exclusions (
                race_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
                bib TEXT NOT NULL,
                PRIMARY KEY (race_id, bib)
            );
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(races)")}
        if "target_seconds" not in columns:
            db.execute("ALTER TABLE races ADD COLUMN target_seconds REAL")
        if "distance_defined" not in columns:
            db.execute("ALTER TABLE races ADD COLUMN distance_defined INTEGER NOT NULL DEFAULT 1")
        db.commit()


class RaceInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    laps: int | None = Field(default=None, gt=0, le=10000)
    target_minutes: float | None = Field(default=None, gt=0, le=100000)
    lap_distance_km: float | None = Field(default=None, gt=0, le=1000)


class ParticipantSetupInput(BaseModel):
    bibs: str = Field(default="", max_length=10000)
    exclusions: str = Field(default="", max_length=10000)


class RaceUpdateInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    laps: int = Field(gt=0, le=10000)
    target_minutes: float | None = Field(default=None, gt=0, le=100000)
    lap_distance_km: float | None = Field(default=None, gt=0, le=1000)


class RaceSelectionInput(BaseModel):
    race_ids: list[int] = Field(min_length=1)


class ParticipantInput(BaseModel):
    bib: str = Field(min_length=1, max_length=30)
    first_name: str = Field(default="", max_length=80)
    last_name: str = Field(default="", max_length=80)


class PassageInput(BaseModel):
    bib: str = Field(min_length=1, max_length=30)


class PassageBibUpdateInput(BaseModel):
    bib: str = Field(min_length=1, max_length=30)


def race_elapsed(row: sqlite3.Row, current: float | None = None) -> float:
    if not row["started_at"]:
        return 0.0
    current_time = current if current is not None else time.time()
    started = datetime.fromisoformat(row["started_at"]).timestamp()
    paused = float(row["paused_seconds"])
    if row["status"] == "paused" and row["paused_at"]:
        paused += max(0.0, current_time - datetime.fromisoformat(row["paused_at"]).timestamp())
    end = current_time
    if row["status"] in {"stopped", "finished"} and row["paused_at"]:
        end = datetime.fromisoformat(row["paused_at"]).timestamp()
    return max(0.0, end - started - paused)


def display_name(participant: sqlite3.Row) -> str:
    name = f"{participant['first_name']} {participant['last_name']}".strip()
    return name or "Sans nom"


def parse_bib_tokens(value: str) -> set[str]:
    bibs: set[str] = set()
    for token in value.split():
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise HTTPException(400, f"Plage de dossards invalide : {token}")
            start, end = int(start_text), int(end_text)
            if start > end or end - start > 10000:
                raise HTTPException(400, f"Plage de dossards invalide : {token}")
            bibs.update(str(number) for number in range(start, end + 1))
        else:
            bibs.add(token)
    return bibs


def bibs_in_other_races(
    db: sqlite3.Connection,
    race_id: int,
    bibs: set[str],
) -> list[str]:
    if not bibs:
        return []
    placeholders = ", ".join("?" for _ in bibs)
    rows = db.execute(
        f"SELECT bib FROM participants "
        f"WHERE race_id != ? AND bib IN ({placeholders}) "
        "ORDER BY CAST(bib AS INTEGER), bib",
        (race_id, *bibs),
    ).fetchall()
    return [row["bib"] for row in rows]


def participant_stats(db: sqlite3.Connection, participant: sqlite3.Row, race: sqlite3.Row) -> dict[str, Any]:
    passages = db.execute(
        "SELECT * FROM passages WHERE participant_id = ? AND cancelled = 0 ORDER BY lap_number",
        (participant["id"],),
    ).fetchall()
    lap_times = [float(item["lap_seconds"]) for item in passages]
    laps_done = len(lap_times)
    total = sum(lap_times)
    latest = lap_times[-1] if lap_times else None
    distance = laps_done * float(race["lap_distance_km"]) if race["distance_defined"] else 0
    average_speed = distance / (total / 3600) if total > 0 else None
    latest_speed = (
        float(race["lap_distance_km"]) / (latest / 3600)
        if latest and latest > 0
        else None
    )
    return {
        "participant_id": participant["id"],
        "bib": participant["bib"],
        "name": display_name(participant),
        "laps_done": laps_done,
        "laps_remaining": max(0, race["laps"] - laps_done),
        "total_seconds": total if passages else None,
        "last_lap_seconds": latest,
        "average_lap_seconds": total / laps_done if laps_done else None,
        "best_lap_seconds": min(lap_times) if lap_times else None,
        "worst_lap_seconds": max(lap_times) if lap_times else None,
        "average_speed_kmh": average_speed,
        "last_lap_speed_kmh": latest_speed,
        "distance_km": distance,
        "distance_remaining_km": (
            max(0, (race["laps"] - laps_done) * race["lap_distance_km"])
            if race["distance_defined"]
            else None
        ),
        "finished": laps_done >= race["laps"] and not (
            race["target_seconds"] and laps_done < 2
        ),
    }


def recalculate_target_laps(db: sqlite3.Connection, race_id: int) -> None:
    race = db.execute(
        "SELECT target_seconds FROM races WHERE id = ?",
        (race_id,),
    ).fetchone()
    if not race or not race["target_seconds"]:
        return
    passages = db.execute(
        "SELECT participant_id, elapsed_seconds FROM passages "
        "WHERE race_id = ? AND cancelled = 0 ORDER BY recorded_at, id",
        (race_id,),
    ).fetchall()
    passage_counts: dict[int, list[float]] = {}
    calculated_laps = 1
    for passage in passages:
        participant_passages = passage_counts.setdefault(passage["participant_id"], [])
        participant_passages.append(float(passage["elapsed_seconds"]))
        if len(participant_passages) == 2:
            average_lap_seconds = participant_passages[1] / 2
            calculated_laps = max(
                2,
                round(float(race["target_seconds"]) / max(average_lap_seconds, 0.001)),
            )
            break
    db.execute(
        "UPDATE races SET laps = ? WHERE id = ?",
        (calculated_laps, race_id),
    )


def race_payload(db: sqlite3.Connection, race: sqlite3.Row) -> dict[str, Any]:
    participants = db.execute(
        "SELECT * FROM participants WHERE race_id = ? ORDER BY CAST(bib AS INTEGER), bib",
        (race["id"],),
    ).fetchall()
    stats = [participant_stats(db, item, race) for item in participants]
    if race["status"] == "ready":
        stats.sort(
            key=lambda item: (
                int(item["bib"]) if item["bib"].isdigit() else float("inf"),
                item["bib"],
            )
        )
    else:
        stats.sort(
            key=lambda item: (
                -item["laps_done"],
                item["total_seconds"] or float("inf"),
                item["bib"],
            )
        )
    exclusions = [
        row["bib"]
        for row in db.execute(
            "SELECT bib FROM race_exclusions WHERE race_id = ? ORDER BY CAST(bib AS INTEGER), bib",
            (race["id"],),
        ).fetchall()
    ]
    leader_laps = stats[0]["laps_done"] if stats else 0
    for position, item in enumerate(stats, 1):
        item["position"] = position
        item["laps_behind"] = leader_laps - item["laps_done"]
    return {
        "id": race["id"],
        "name": race["name"],
        "laps": race["laps"],
        "target_seconds": race["target_seconds"],
        "lap_distance_km": race["lap_distance_km"] if race["distance_defined"] else None,
        "status": race["status"],
        "elapsed_seconds": race_elapsed(race),
        "started_at": race["started_at"],
        "participant_count": len(stats),
        "bibs": [item["bib"] for item in stats],
        "exclusions": exclusions,
        "finished_count": sum(item["finished"] for item in stats),
        "standings": stats,
    }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML


@app.get("/favicon.png", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(BASE_DIR / "app" / "static" / "favicon_ffc.png", media_type="image/png")


@app.get("/logo-ffc.png", include_in_schema=False)
def ffc_logo() -> FileResponse:
    return FileResponse(BASE_DIR / "app" / "static" / "Logo_FFC.png", media_type="image/png")


@app.get("/api/races")
def list_races() -> list[dict[str, Any]]:
    with closing(connect()) as db:
        return [race_payload(db, race) for race in db.execute("SELECT * FROM races ORDER BY id").fetchall()]


@app.post("/api/races")
def create_race(data: RaceInput) -> dict[str, Any]:
    if not data.name.strip():
        raise HTTPException(400, "Le nom de la course est obligatoire")
    if (data.laps is None) == (data.target_minutes is None):
        raise HTTPException(400, "Renseignez le nombre de tours")
    name = data.name.strip()
    with closing(connect()) as db:
        if db.execute("SELECT 1 FROM races WHERE lower(name) = lower(?)", (name,)).fetchone():
            raise HTTPException(409, "Le nom de cette course existe déjà")
        cursor = db.execute(
            "INSERT INTO races(name, laps, target_seconds, lap_distance_km, distance_defined, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                name,
                data.laps or 1,
                data.target_minutes * 60 if data.target_minutes else None,
                data.lap_distance_km or 1,
                1 if data.lap_distance_km else 0,
                now_iso(),
            ),
        )
        db.commit()
        return {"id": cursor.lastrowid}


@app.delete("/api/races/{race_id}")
def delete_race(race_id: int) -> dict[str, bool]:
    with closing(connect()) as db:
        result = db.execute("DELETE FROM races WHERE id = ?", (race_id,))
        db.commit()
        if result.rowcount == 0:
            raise HTTPException(404, "Course inconnue")
        return {"ok": True}


@app.patch("/api/races/{race_id}/details")
def update_race_details(race_id: int, data: RaceUpdateInput) -> dict[str, Any]:
    name = data.name.strip()
    with closing(connect()) as db:
        race = db.execute("SELECT id FROM races WHERE id = ?", (race_id,)).fetchone()
        if not race:
            raise HTTPException(404, "Course inconnue")
        if db.execute(
            "SELECT 1 FROM races WHERE lower(name) = lower(?) AND id != ?",
            (name, race_id),
        ).fetchone():
            raise HTTPException(409, "Le nom de cette course existe déjà")
        db.execute(
            "UPDATE races SET name = ?, laps = ?, target_seconds = ?, "
            "lap_distance_km = ?, distance_defined = ? WHERE id = ?",
            (
                name,
                data.laps,
                data.target_minutes * 60 if data.target_minutes else None,
                data.lap_distance_km or 1,
                1 if data.lap_distance_km else 0,
                race_id,
            ),
        )
        db.commit()
        return {"id": race_id, "name": name}


@app.post("/api/races/{race_id}/participants")
def add_participant(race_id: int, data: ParticipantInput) -> dict[str, Any]:
    with closing(connect()) as db:
        if not db.execute("SELECT 1 FROM races WHERE id = ?", (race_id,)).fetchone():
            raise HTTPException(404, "Course inconnue")
        bib = data.bib.strip()
        if bibs_in_other_races(db, race_id, {bib}):
            raise HTTPException(409, "Ce dossard est déjà attribué à une autre course")
        try:
            cursor = db.execute(
                "INSERT INTO participants(race_id, bib, first_name, last_name) VALUES (?, ?, ?, ?)",
                (race_id, bib, data.first_name.strip(), data.last_name.strip()),
            )
            db.commit()
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Ce dossard existe déjà dans cette course") from exc
        return {"id": cursor.lastrowid}


@app.post("/api/races/{race_id}/participants/setup")
def setup_participants(race_id: int, data: ParticipantSetupInput) -> dict[str, int]:
    bibs = parse_bib_tokens(data.bibs)
    exclusions = parse_bib_tokens(data.exclusions)
    with closing(connect()) as db:
        if not db.execute("SELECT 1 FROM races WHERE id = ?", (race_id,)).fetchone():
            raise HTTPException(404, "Course inconnue")
        existing_exclusions = {
            row["bib"]
            for row in db.execute(
                "SELECT bib FROM race_exclusions WHERE race_id = ?",
                (race_id,),
            ).fetchall()
        }
        bibs_to_add = bibs - exclusions
        conflicts = bibs_in_other_races(db, race_id, bibs_to_add)
        if conflicts:
            raise HTTPException(
                409,
                "Les dossards sont déjà attribués à une autre course : "
                + ", ".join(conflicts),
            )
        for bib in bibs_to_add & existing_exclusions:
            db.execute(
                "DELETE FROM race_exclusions WHERE race_id = ? AND bib = ?",
                (race_id, bib),
            )
        exclusions_to_add = exclusions - existing_exclusions
        protected = []
        for bib in exclusions_to_add:
            participant = db.execute(
                "SELECT id, bib FROM participants WHERE race_id = ? AND bib = ?",
                (race_id, bib),
            ).fetchone()
            if participant and db.execute(
                "SELECT 1 FROM passages WHERE participant_id = ? LIMIT 1",
                (participant["id"],),
            ).fetchone():
                protected.append(participant)
        if protected:
            protected_bibs = ", ".join(item["bib"] for item in protected)
            raise HTTPException(
                409,
                f"Impossible d'exclure les dossards déjà enregistrés : {protected_bibs}",
            )
        for bib in exclusions_to_add:
            db.execute(
                "INSERT INTO race_exclusions(race_id, bib) VALUES (?, ?)",
                (race_id, bib),
            )
            db.execute(
                "DELETE FROM participants WHERE race_id = ? AND bib = ?",
                (race_id, bib),
            )
        added = 0
        for bib in bibs_to_add:
            try:
                db.execute(
                    "INSERT INTO participants(race_id, bib) VALUES (?, ?)",
                    (race_id, bib),
                )
                added += 1
            except sqlite3.IntegrityError:
                continue
        db.commit()
    return {"added": added, "excluded": len(exclusions)}


@app.post("/api/races/{race_id}/participants/import")
async def import_participants(race_id: int, file: UploadFile = File(...)) -> dict[str, int]:
    raw = await file.read()
    filename = (file.filename or "").lower()
    if filename.endswith(".xlsx"):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise HTTPException(500, "Le support Excel nécessite openpyxl") from exc
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        headers = [str(value or "").strip().lower() for value in next(rows, ())]
        if "bib" not in headers:
            raise HTTPException(400, "Excel doit contenir une colonne bib")
        records = (
            dict(zip(headers, (str(value or "") for value in row)))
            for row in rows
        )
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "Le fichier doit être un CSV UTF-8 ou un fichier Excel .xlsx") from exc
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "bib" not in [item.strip().lower() for item in reader.fieldnames]:
            raise HTTPException(400, "Le CSV doit contenir une colonne bib")
        records = (
            {key.strip().lower(): value for key, value in row.items()}
            for row in reader
        )
    records = list(records)
    added = 0
    with closing(connect()) as db:
        if not db.execute("SELECT 1 FROM races WHERE id = ?", (race_id,)).fetchone():
            raise HTTPException(404, "Course inconnue")
        imported_bibs = {
            (row.get("bib") or "").strip()
            for row in records
            if (row.get("bib") or "").strip()
        }
        conflicts = bibs_in_other_races(db, race_id, imported_bibs)
        if conflicts:
            raise HTTPException(
                409,
                "Les dossards sont déjà attribués à une autre course : "
                + ", ".join(conflicts),
            )
        for row in records:
            bib = (row.get("bib") or "").strip()
            if not bib:
                continue
            try:
                db.execute(
                    "INSERT INTO participants(race_id, bib, first_name, last_name) VALUES (?, ?, ?, ?)",
                    (race_id, bib, (row.get("first_name") or "").strip(), (row.get("last_name") or "").strip()),
                )
                added += 1
            except sqlite3.IntegrityError:
                continue
        db.commit()
    return {"added": added}


@app.post("/api/races/{race_id}/timer/{action}")
def timer_action(race_id: int, action: str) -> dict[str, Any]:
    if action not in {"start", "pause", "resume", "stop"}:
        raise HTTPException(400, "Action de chronomètre invalide")
    with closing(connect()) as db:
        race = db.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()
        if not race:
            raise HTTPException(404, "Course inconnue")
        current = time.time()
        status = race["status"]
        started_at = race["started_at"]
        paused_at = race["paused_at"]
        paused_seconds = race["paused_seconds"]
        if action == "start":
            if status != "ready":
                raise HTTPException(409, "La course a déjà démarré")
            started_at, status = now_iso(), "running"
        elif action == "pause":
            if status != "running":
                raise HTTPException(409, "La course n'est pas en cours")
            paused_at, status = now_iso(), "paused"
        elif action == "resume":
            if status != "paused":
                raise HTTPException(409, "La course n'est pas en pause")
            paused_seconds += current - datetime.fromisoformat(paused_at).timestamp()
            paused_at, status = None, "running"
        elif action == "stop":
            if status not in {"running", "paused"}:
                raise HTTPException(409, "La course ne peut pas être arrêtée")
            if status == "running":
                paused_at = now_iso()
            status = "stopped"
        db.execute(
            "UPDATE races SET status = ?, started_at = ?, paused_at = ?, paused_seconds = ? WHERE id = ?",
            (status, started_at, paused_at, paused_seconds, race_id),
        )
        db.commit()
        updated = db.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()
        return race_payload(db, updated)


@app.post("/api/races/timer/start-selected")
def start_selected_timers(data: RaceSelectionInput) -> dict[str, Any]:
    race_ids = list(dict.fromkeys(data.race_ids))
    started_at = now_iso()
    with closing(connect()) as db:
        placeholders = ",".join("?" for _ in race_ids)
        races = db.execute(
            f"SELECT id, status FROM races WHERE id IN ({placeholders})",
            race_ids,
        ).fetchall()
        found_ids = {row["id"] for row in races}
        missing_ids = [race_id for race_id in race_ids if race_id not in found_ids]
        if missing_ids:
            raise HTTPException(404, "Une des courses sélectionnées est inconnue")
        not_ready = [row["id"] for row in races if row["status"] != "ready"]
        if not_ready:
            raise HTTPException(409, "Toutes les courses sélectionnées doivent être en préparation")
        db.execute(
            f"UPDATE races SET status = 'running', started_at = ? "
            f"WHERE id IN ({placeholders})",
            [started_at, *race_ids],
        )
        db.commit()
        return {"started": race_ids}


@app.post("/api/passages")
def record_passage(data: PassageInput) -> dict[str, Any]:
    with closing(connect()) as db:
        participant = db.execute(
            "SELECT p.*, r.status, r.started_at, r.paused_at, r.paused_seconds, "
            "r.laps, r.target_seconds, r.lap_distance_km, r.name "
            "FROM participants p JOIN races r ON r.id = p.race_id WHERE p.bib = ?",
            (data.bib.strip(),),
        ).fetchone()
        if not participant:
            raise HTTPException(404, "Dossard inconnu")
        if participant["status"] not in {"running"}:
            raise HTTPException(409, f"Le chronomètre de « {participant['name']} » n'est pas en cours")
        previous = db.execute(
            "SELECT * FROM passages WHERE participant_id = ? AND cancelled = 0 ORDER BY lap_number DESC LIMIT 1",
            (participant["id"],),
        ).fetchone()
        if (
            previous
            and previous["lap_number"] >= participant["laps"]
            and not (participant["target_seconds"] and participant["laps"] == 1)
        ):
            raise HTTPException(409, "Ce coureur a déjà terminé")
        race = db.execute("SELECT * FROM races WHERE id = ?", (participant["race_id"],)).fetchone()
        elapsed = race_elapsed(race)
        lap_number = (previous["lap_number"] if previous else 0) + 1
        lap_seconds = elapsed - (previous["elapsed_seconds"] if previous else 0)
        cursor = db.execute(
            "INSERT INTO passages(race_id, participant_id, recorded_at, elapsed_seconds, lap_number, lap_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (participant["race_id"], participant["id"], now_iso(), elapsed, lap_number, max(0, lap_seconds)),
        )
        recalculate_target_laps(db, race["id"])
        db.commit()
        return {
            "id": cursor.lastrowid,
            "race_id": participant["race_id"],
            "race_name": participant["name"],
            "bib": participant["bib"],
            "lap_number": lap_number,
            "lap_seconds": lap_seconds,
        }


@app.post("/api/passages/{passage_id}/cancel")
def cancel_passage(passage_id: int) -> dict[str, bool]:
    with closing(connect()) as db:
        passage = db.execute(
            "SELECT race_id FROM passages WHERE id = ? AND cancelled = 0",
            (passage_id,),
        ).fetchone()
        if not passage:
            raise HTTPException(404, "Passage introuvable")
        result = db.execute("UPDATE passages SET cancelled = 1 WHERE id = ?", (passage_id,))
        recalculate_target_laps(db, passage["race_id"])
        db.commit()
        return {"ok": True}


@app.patch("/api/passages/{passage_id}")
def update_passage_bib(passage_id: int, data: PassageBibUpdateInput) -> dict[str, Any]:
    bib = data.bib.strip()
    with closing(connect()) as db:
        passage = db.execute(
            "SELECT p.id, p.race_id FROM passages p WHERE p.id = ? AND p.cancelled = 0",
            (passage_id,),
        ).fetchone()
        if not passage:
            raise HTTPException(404, "Passage introuvable")
        participant = db.execute(
            "SELECT id, bib FROM participants WHERE race_id = ? AND bib = ?",
            (passage["race_id"], bib),
        ).fetchone()
        if not participant:
            raise HTTPException(404, "Ce dossard n'existe pas dans cette course")
        db.execute(
            "UPDATE passages SET participant_id = ? WHERE id = ?",
            (participant["id"], passage_id),
        )
        recalculate_target_laps(db, passage["race_id"])
        db.commit()
    return {"id": passage_id, "bib": participant["bib"]}


@app.get("/api/passages/recent")
def recent_passages() -> list[dict[str, Any]]:
    with closing(connect()) as db:
        return [dict(row) for row in db.execute(
            "SELECT x.id, x.race_id, x.bib, x.race_name, x.lap_number, x.lap_seconds, x.recorded_at FROM ("
            "SELECT p.*, u.bib, r.name AS race_name FROM passages p "
            "JOIN participants u ON u.id = p.participant_id "
            "JOIN races r ON r.id = p.race_id "
            "WHERE p.cancelled = 0 "
            "ORDER BY p.id DESC LIMIT 30) x ORDER BY x.id DESC"
        ).fetchall()]


HTML = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="icon" type="image/png" href="/favicon.png">
<title>Chronométrage cycliste</title>
<style>
:root{font-family:Inter,Segoe UI,sans-serif;color:#172033;background:#f2f5f8}*{box-sizing:border-box}
body{margin:0}.top{background:#10233f;color:white;padding:18px 24px;display:flex;justify-content:space-between;align-items:center}
h1,h2,h3{margin:0 0 12px}.top h1{font-size:22px}.layout{max-width:1500px;margin:auto;padding:20px}
.inputbar{position:sticky;top:0;z-index:2;background:white;padding:16px;border-radius:12px;box-shadow:0 2px 12px #13233d22;margin-bottom:20px}
.inputbar input{font-size:28px;width:180px;padding:8px 12px;border:2px solid #aeb9c8;border-radius:8px}.inputbar button,.button{border:0;border-radius:8px;padding:11px 16px;font-weight:700;cursor:pointer;background:#1769aa;color:#fff}
.inputbar button{font-size:20px;margin-left:8px}.message{margin-left:15px;font-weight:700}.ok{color:#16803c}.error{color:#c62828}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(520px,1fr));gap:18px}.card{background:white;border-radius:12px;padding:18px;box-shadow:0 2px 8px #13233d12}
.card-head{display:flex;justify-content:space-between;gap:12px}.timer{font:700 28px ui-monospace,monospace;color:#1769aa}.status{font-size:12px;text-transform:uppercase;font-weight:700;padding:5px 8px;border-radius:20px;background:#e8edf3}
.controls{display:flex;gap:7px;flex-wrap:wrap;margin:12px 0}.controls button{padding:8px 12px;border:1px solid #c8d1dc;background:#fff;border-radius:7px;cursor:pointer}.controls button.primary{background:#1769aa;color:#fff}.select-race{display:flex;align-items:center;gap:5px;padding:8px 4px;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:8px 5px;border-bottom:1px solid #e8edf3}th{color:#59697e}.standings-scroll{max-height:360px;overflow-y:auto}.muted{color:#68778a;font-size:13px}.form{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.form input{padding:8px;border:1px solid #bcc8d5;border-radius:6px;min-width:100px}.section{margin-top:20px}.recent{max-height:250px;overflow:auto}
.footer{margin-top:30px;padding:22px 24px;background:white;display:flex;justify-content:space-between;align-items:center;gap:20px;color:#59697e}.footer-logo{width:130px;height:70px;object-fit:contain}
@media(max-width:650px){.layout{padding:10px}.grid{grid-template-columns:1fr}.top{padding:14px}.inputbar input{width:130px}.message{display:block;margin:10px 0 0}}
</style></head>
<body><header class="top"><h1>Chronométrage cycliste</h1><span>Classement provisoire</span></header>
<main class="layout">
<section class="inputbar"><strong>Saisie rapide</strong><input id="bib" autofocus inputmode="numeric" placeholder="Dossard"><button onclick="record()">Entrée</button><span id="message" class="message"></span></section>
<section class="card"><h2>Créer une course</h2><form class="form" onsubmit="createRace(event)">
<input id="raceName" placeholder="Nom de la course" required>
<label><input type="radio" name="raceMode" value="laps" checked onchange="toggleRaceMode()"> Nombre de tours</label>
<label><input type="radio" name="raceMode" value="target" onchange="toggleRaceMode()"> Temps visé</label>
<input id="raceLaps" type="number" min="1" placeholder="Tours">
<input id="raceTarget" type="number" min="0.1" step="0.1" placeholder="Minutes visées" style="display:none">
<input id="raceDistance" type="number" min="0.01" step="0.01" placeholder="Km/tour (optionnel)">
<button class="button">Suivant</button></form></section>
<section id="sync-controls" class="card section" style="display:none"><button class="button" onclick="startSelected()">Démarrer les courses sélectionnées ensemble</button><span class="muted"> Cochez les courses à lancer au même moment.</span></section>
<section class="card section"><h2>Derniers passages</h2><div id="recent" class="recent"></div></section>
<section id="races" class="grid section"></section>
</main>
<footer class="footer"><span>Créé par Cedric Grandvaux · Si vous voyez une erreur ou avez des améliorations, contactez-moi par mail : <a href="mailto:cedric.grandvaux@gmail.com">cedric.grandvaux@gmail.com</a></span><img class="footer-logo" src="/logo-ffc.png" alt="Logo FFC"></footer>
<script>
const $=id=>document.getElementById(id);
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const fmt=s=>{if(s==null)return'—';const total=Math.max(0,Math.floor(Number(s)*100));const minutes=Math.floor(total/6000);const seconds=Math.floor(total/100)%60;const centiseconds=total%100;return `${String(minutes).padStart(2,'0')}:${String(seconds).padStart(2,'0')}.${String(centiseconds).padStart(2,'0')}`};
const num=(n,d=1)=>n==null?'—':Number(n).toFixed(d);
function errorMessage(detail){if(typeof detail==='string')return detail;if(Array.isArray(detail)){const labels={name:'nom de la course',laps:'nombre de tours',target_minutes:'temps visé',lap_distance_km:'distance par tour',bibs:'dossards',exclusions:'exclusions',race_ids:'courses sélectionnées',bib:'dossard'};return detail.map(item=>{const field=Array.isArray(item.loc)?item.loc[item.loc.length-1]:'';return field?`${labels[field]||field}: ${item.msg}`:item.msg}).join(' · ')}if(detail&&typeof detail==='object')return detail.msg||detail.message||JSON.stringify(detail);return 'Une erreur est survenue'}
async function api(url,opt={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opt});let x;try{x=await r.json()}catch{throw Error(`Erreur serveur (${r.status})`)}if(!r.ok)throw Error(errorMessage(x.detail));return x}
function flash(text,ok=false){$('message').textContent=text;$('message').className='message '+(ok?'ok':'error');setTimeout(()=>$('message').textContent='',3500)}
function toggleRaceMode(){const target=document.querySelector('input[name="raceMode"]:checked').value==='target';$('raceLaps').style.display=target?'none':'';$('raceTarget').style.display=target?'':'none';$('raceLaps').required=!target;$('raceTarget').required=target}
async function record(){const input=$('bib');if(!input.value.trim())return;try{const x=await api('/api/passages',{method:'POST',body:JSON.stringify({bib:input.value.trim()})});flash(`✓ ${x.bib} · ${x.race_name} · tour ${x.lap_number} · ${fmt(x.lap_seconds)}`,true);input.value='';await refresh()}catch(e){flash('⚠ '+e.message);input.select()}}
async function createRace(e){e.preventDefault();try{const target=document.querySelector('input[name="raceMode"]:checked').value==='target';const name=$('raceName').value.trim();const laps=$('raceLaps').value.trim();const targetMinutes=$('raceTarget').value.trim();if(!name){flash('Renseignez le nom de la course');return}if(target&&!targetMinutes){flash('Renseignez le temps visé');return}if(!target&&!laps){flash('Renseignez le nombre de tours');return}const race=await api('/api/races',{method:'POST',body:JSON.stringify({name,laps:target?null:Number(laps),target_minutes:target?Number(targetMinutes):null,lap_distance_km:$('raceDistance').value?Number($('raceDistance').value):null})});e.target.reset();toggleRaceMode();await refresh();document.querySelector(`[data-race-id="${race.id}"]`)?.scrollIntoView({behavior:'smooth'})}catch(e){flash(e.message)}}
async function timer(id,action){try{await api(`/api/races/${id}/timer/${action}`,{method:'POST'});refresh()}catch(e){flash(e.message)}}
async function startSelected(){const ids=[...document.querySelectorAll('.race-select:checked')].map(input=>Number(input.value));if(!ids.length){flash('Sélectionnez au moins une course');return}try{await api('/api/races/timer/start-selected',{method:'POST',body:JSON.stringify({race_ids:ids})});flash('Chronomètres démarrés ensemble',true);await refresh()}catch(e){flash(e.message)}}
async function editRace(id,button){const card=button.closest('[data-race-id]');const name=prompt('Nom de la course :',card.querySelector('h2').textContent);if(name===null||!name.trim())return;const laps=prompt('Nombre de tours :',card.dataset.laps);if(laps===null||!laps.trim()||Number(laps)<=0){flash('Renseignez un nombre de tours valide');return}const distance=prompt('Km par tour (laisser vide pour ne pas afficher la vitesse moyenne) :',card.dataset.distance||'');if(distance===null)return;const target=card.dataset.target?Number(card.dataset.target):null;const targetValue=target===null?null:prompt('Temps visé en minutes :',target);if(target!==null&&(targetValue===null||!targetValue.trim()||Number(targetValue)<=0)){flash('Renseignez un temps visé valide');return}try{await api(`/api/races/${id}/details`,{method:'PATCH',body:JSON.stringify({name:name.trim(),laps:Number(laps),target_minutes:targetValue===null?null:Number(targetValue),lap_distance_km:distance.trim()?Number(distance):null})});flash('Caractéristiques modifiées',true);await refresh()}catch(e){flash(e.message)}}
async function removeRace(id,button){const name=button.closest('[data-race-id]').querySelector('h2').textContent;if(!confirm(`Supprimer la course « ${name} » et tout son historique ?`))return;try{await api(`/api/races/${id}`,{method:'DELETE'});flash('Course supprimée',true);await refresh()}catch(e){flash(e.message)}}
async function updatePassage(id,button){const current=button.parentElement.querySelector('b').textContent;const bib=prompt('Nouveau dossard pour ce passage :',current);if(bib===null||!bib.trim()||bib.trim()===current)return;try{await api(`/api/passages/${id}`,{method:'PATCH',body:JSON.stringify({bib:bib.trim()})});flash('Dossard du passage modifié',true);await refresh()}catch(e){flash(e.message)}}
async function setupParticipants(id,form){const fd=new FormData(form);try{await api(`/api/races/${id}/participants/setup`,{method:'POST',body:JSON.stringify({bibs:fd.get('bibs'),exclusions:fd.get('exclusions')})});form.reset();flash('Dossards et exclusions enregistrés',true);await refresh()}catch(e){flash(e.message)}}
async function cancelPassage(id){try{await api(`/api/passages/${id}/cancel`,{method:'POST'});flash('Dernier passage annulé',true);refresh()}catch(e){flash(e.message)}}
function row(x,showSpeed){return `<tr><td>${x.position}</td><td><b>${escapeHtml(x.bib)}</b></td><td>${x.laps_done}/${x.laps_done+x.laps_remaining}</td><td>${x.laps_remaining}</td><td>${x.laps_behind?'−'+x.laps_behind+' tour':'—'}</td><td>${fmt(x.last_lap_seconds)}</td>${showSpeed?`<td>${num(x.average_speed_kmh)} km/h</td>`:''}</tr>`}
function raceCard(r){let buttons=r.status==='ready'?`<button class="primary" onclick="timer(${r.id},'start')">Démarrer</button>`:r.status==='running'?`<button onclick="timer(${r.id},'pause')">Pause</button>`:r.status==='paused'?`<button class="primary" onclick="timer(${r.id},'resume')">Reprendre</button><button onclick="timer(${r.id},'stop')">Arrêter</button>`:'';let selection=r.status==='ready'?`<label class="select-race"><input class="race-select" type="checkbox" value="${r.id}"> Lancer avec la sélection</label>`:'';let laps=r.target_seconds?(r.laps>1?`${r.laps} tours`:`tours calculés après 2 tours · objectif ${Math.round(r.target_seconds/60)} min`):`${r.laps} tours`;let distance=r.lap_distance_km==null?'distance non définie':`${r.lap_distance_km} km/tour`;let standingsClass=r.standings.length>10?'standings-scroll':'';let showSpeed=r.lap_distance_km!=null;let speedHeader=showSpeed?'<th>Vitesse moy.</th>':'';let emptyColumns=showSpeed?7:6;return `<article class="card" data-race-id="${r.id}" data-laps="${r.laps}" data-target="${r.target_seconds?Math.round(r.target_seconds/60):''}" data-distance="${r.lap_distance_km??''}"><div class="card-head"><div><h2>${escapeHtml(r.name)}</h2><div class="muted">${laps} · ${escapeHtml(distance)} · ${r.participant_count} participants · ${r.finished_count} terminé(s)</div></div><div><div class="timer" data-elapsed="${r.elapsed_seconds}" data-status="${r.status}">${fmt(r.elapsed_seconds)}</div><span class="status">${r.status}</span></div></div><div class="controls">${buttons}${selection}<button onclick="editRace(${r.id},this)">Modifier</button><button onclick="removeRace(${r.id},this)">Supprimer</button></div><form class="form" onsubmit="setupParticipants(${r.id},this);return false"><input name="bibs" placeholder="Dossards : 1 2 3 ou 4-65"><input name="exclusions" placeholder="Exclus : 7 12 ou 40-45"><button class="button">Enregistrer les dossards</button></form><div class="${standingsClass}"><table><thead><tr><th>#</th><th>Dossard</th><th>Tours</th><th>Tours restants</th><th>Retard</th><th>Dernier tour</th>${speedHeader}</tr></thead><tbody>${r.standings.map(item=>row(item,showSpeed)).join('')||`<tr><td colspan="${emptyColumns}" class="muted">Configurez les dossards de cette course.</td></tr>`}</tbody></table></div></article>`}
let timerSyncAt=performance.now();
function updateTimers(){const elapsed=performance.now()-timerSyncAt;document.querySelectorAll('.timer[data-elapsed]').forEach(timer=>{const base=Number(timer.dataset.elapsed);const value=timer.dataset.status==='running'?base+elapsed/1000:base;timer.textContent=fmt(value)})}
async function refresh(){if(document.activeElement&&document.activeElement.matches('input,textarea'))return;const scrollPositions={};document.querySelectorAll('[data-race-id] .standings-scroll').forEach(element=>{scrollPositions[element.closest('[data-race-id]').dataset.raceId]=element.scrollTop});const [races,recent]=await Promise.all([api('/api/races'),api('/api/passages/recent')]);$('sync-controls').style.display=races.filter(r=>r.status==='ready').length>=2?'block':'none';$('races').innerHTML=races.map(raceCard).join('');Object.entries(scrollPositions).forEach(([raceId,scrollTop])=>{const element=document.querySelector(`[data-race-id="${raceId}"] .standings-scroll`);if(element)element.scrollTop=scrollTop});timerSyncAt=performance.now();updateTimers();$('recent').innerHTML=recent.map(x=>`<div><b>${escapeHtml(x.bib)}</b> · ${escapeHtml(x.race_name)} · tour ${x.lap_number} · ${fmt(x.lap_seconds)} <button onclick="updatePassage(${x.id},this)">Modifier</button> <button onclick="cancelPassage(${x.id})">Annuler</button></div>`).join('')||'<span class="muted">Aucun passage.</span>'}
$('bib').addEventListener('keydown',e=>{if(e.key==='Enter')record()});refresh();setInterval(refresh,1000);setInterval(updateTimers,10);
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
