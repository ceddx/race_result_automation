from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import main


@pytest.fixture
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "DATABASE", tmp_path / "test.sqlite3")
    main.init_db()


def create_race(
    name: str = "Test race",
    *,
    laps: int | None = 5,
    target_minutes: float | None = None,
    distance: float | None = None,
) -> int:
    return main.create_race(
        main.RaceInput(
            name=name,
            laps=laps,
            target_minutes=target_minutes,
            lap_distance_km=distance,
        )
    )["id"]


def set_race_running(race_id: int) -> None:
    started_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with main.connect() as db:
        db.execute(
            "UPDATE races SET status = 'running', started_at = ? WHERE id = ?",
            (started_at, race_id),
        )
        db.commit()


def test_races_require_a_duration_and_names_are_unique(
    isolated_database: None,
) -> None:
    with pytest.raises(main.HTTPException, match="Renseignez le nombre de tours"):
        main.create_race(main.RaceInput(name="Missing duration"))

    create_race("Road race", laps=10)
    with pytest.raises(main.HTTPException) as error:
        create_race("road race", laps=8)
    assert error.value.status_code == 409


def test_bib_ranges_and_exclusions_are_applied(isolated_database: None) -> None:
    race_id = create_race("Track race")

    main.setup_participants(
        race_id,
        main.ParticipantSetupInput(bibs="1 2 4-6", exclusions="2 5"),
    )

    race = main.list_races()[0]
    assert race["bibs"] == ["1", "4", "6"]
    assert race["exclusions"] == ["2", "5"]


def test_bib_setup_adds_multiple_batches(isolated_database: None) -> None:
    race_id = create_race("Multiple batches")

    main.setup_participants(
        race_id,
        main.ParticipantSetupInput(bibs="1-3", exclusions="9"),
    )
    main.setup_participants(
        race_id,
        main.ParticipantSetupInput(bibs="4 5", exclusions="10-11"),
    )

    race = main.list_races()[0]
    assert race["bibs"] == ["1", "2", "3", "4", "5"]
    assert race["exclusions"] == ["9", "10", "11"]


def test_bib_setup_can_reactivate_a_previously_excluded_bib(
    isolated_database: None,
) -> None:
    race_id = create_race("Reactivated bib")

    main.setup_participants(
        race_id,
        main.ParticipantSetupInput(bibs="1-5", exclusions="3"),
    )
    main.setup_participants(
        race_id,
        main.ParticipantSetupInput(bibs="3"),
    )

    race = main.list_races()[0]
    assert race["bibs"] == ["1", "2", "3", "4", "5"]
    assert race["exclusions"] == []


def test_a_bib_can_only_be_assigned_to_one_race(
    isolated_database: None,
) -> None:
    first_race = create_race("First race")
    second_race = create_race("Second race")
    main.setup_participants(first_race, main.ParticipantSetupInput(bibs="42"))

    with pytest.raises(main.HTTPException, match="autre course"):
        main.setup_participants(
            second_race,
            main.ParticipantSetupInput(bibs="42"),
        )

    with pytest.raises(main.HTTPException, match="autre course"):
        main.add_participant(
            second_race,
            main.ParticipantInput(bib="42"),
        )


def test_ready_race_lists_bibs_in_ascending_order(
    isolated_database: None,
) -> None:
    race_id = create_race("Ascending bibs")

    main.setup_participants(
        race_id,
        main.ParticipantSetupInput(bibs="12 3 1 20 2"),
    )

    assert main.list_races()[0]["bibs"] == ["1", "2", "3", "12", "20"]


def test_invalid_bib_ranges_are_rejected(isolated_database: None) -> None:
    with pytest.raises(main.HTTPException, match="Plage de dossards invalide"):
        main.parse_bib_tokens("10-4")

    with pytest.raises(main.HTTPException, match="Plage de dossards invalide"):
        main.parse_bib_tokens("4-invalid")


def test_timer_lifecycle(isolated_database: None) -> None:
    race_id = create_race()

    started = main.timer_action(race_id, "start")
    assert started["status"] == "running"
    paused = main.timer_action(race_id, "pause")
    assert paused["status"] == "paused"
    resumed = main.timer_action(race_id, "resume")
    assert resumed["status"] == "running"
    stopped = main.timer_action(race_id, "stop")
    assert stopped["status"] == "stopped"


def test_selected_races_start_together(isolated_database: None) -> None:
    first = create_race("A")
    second = create_race("B")
    third = create_race("C")

    result = main.start_selected_timers(main.RaceSelectionInput(race_ids=[first, third]))

    assert result["started"] == [first, third]
    races = {race["id"]: race for race in main.list_races()}
    assert races[first]["status"] == "running"
    assert races[second]["status"] == "ready"
    assert races[third]["status"] == "running"
    assert races[first]["started_at"] == races[third]["started_at"]


def test_passages_increment_laps_and_are_cancelled(
    isolated_database: None,
) -> None:
    race_id = create_race(distance=1.5)
    main.setup_participants(
        race_id,
        main.ParticipantSetupInput(bibs="12"),
    )
    set_race_running(race_id)

    passage = main.record_passage(main.PassageInput(bib="12"))
    assert passage["lap_number"] == 1
    assert passage["lap_seconds"] >= 0

    main.cancel_passage(passage["id"])
    race = main.list_races()[0]
    assert race["standings"][0]["laps_done"] == 0
    assert main.recent_passages() == []


def test_bulk_passage_records_active_participants_at_same_time(
    isolated_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    race_id = create_race("Peloton race", laps=3)
    main.setup_participants(race_id, main.ParticipantSetupInput(bibs="1 2 3"))
    set_race_running(race_id)
    monkeypatch.setattr(main, "race_elapsed", lambda row: 42.0)

    result = main.record_bulk_passages(race_id)

    assert result["added"] == 3
    with main.connect() as db:
        rows = db.execute(
            "SELECT elapsed_seconds, lap_number FROM passages "
            "WHERE race_id = ? ORDER BY participant_id",
            (race_id,),
        ).fetchall()
    assert [(row["elapsed_seconds"], row["lap_number"]) for row in rows] == [
        (42.0, 1),
        (42.0, 1),
        (42.0, 1),
    ]


def test_bulk_passage_skips_finished_participants(
    isolated_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    race_id = create_race("Peloton finish", laps=1)
    main.setup_participants(race_id, main.ParticipantSetupInput(bibs="1 2"))
    set_race_running(race_id)
    monkeypatch.setattr(main, "race_elapsed", lambda row: 42.0)
    main.record_passage(main.PassageInput(bib="1"))

    result = main.record_bulk_passages(race_id)

    assert result["added"] == 1
    assert result["passages"][0]["bib"] == "2"


def test_passage_bib_can_be_corrected_within_the_same_race(
    isolated_database: None,
) -> None:
    race_id = create_race()
    main.setup_participants(race_id, main.ParticipantSetupInput(bibs="12 13"))
    set_race_running(race_id)

    passage = main.record_passage(main.PassageInput(bib="12"))
    updated = main.update_passage_bib(
        passage["id"],
        main.PassageBibUpdateInput(bib="13"),
    )

    assert updated["bib"] == "13"
    assert main.recent_passages()[0]["bib"] == "13"


def test_passage_correction_renumbers_remaining_passages(
    isolated_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    race_id = create_race(laps=7)
    main.setup_participants(race_id, main.ParticipantSetupInput(bibs="1 2"))
    set_race_running(race_id)

    def elapsed(row: object) -> float:
        with main.connect() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM passages WHERE race_id = ? AND cancelled = 0",
                (race_id,),
            ).fetchone()[0]
        return (count + 1) * 10.0

    monkeypatch.setattr(main, "race_elapsed", elapsed)
    passages = [
        main.record_passage(main.PassageInput(bib="1"))
        for _ in range(6)
    ]

    main.update_passage_bib(
        passages[2]["id"],
        main.PassageBibUpdateInput(bib="2"),
    )
    next_passage = main.record_passage(main.PassageInput(bib="1"))

    assert next_passage["lap_number"] == 6
    standings = {item["bib"]: item for item in main.list_races()[0]["standings"]}
    assert standings["1"]["laps_done"] == 6
    assert standings["1"]["laps_remaining"] == 1


def test_unknown_or_finished_bibs_are_rejected(isolated_database: None) -> None:
    race_id = create_race(laps=1)
    main.setup_participants(race_id, main.ParticipantSetupInput(bibs="7"))
    set_race_running(race_id)

    with pytest.raises(main.HTTPException, match="Dossard inconnu"):
        main.record_passage(main.PassageInput(bib="99"))

    main.record_passage(main.PassageInput(bib="7"))
    with pytest.raises(main.HTTPException, match="déjà terminé"):
        main.record_passage(main.PassageInput(bib="7"))


def test_target_duration_calculates_laps_from_second_passage(
    isolated_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    race_id = create_race("Target race", laps=None, target_minutes=10)
    main.setup_participants(race_id, main.ParticipantSetupInput(bibs="1 2"))
    set_race_running(race_id)
    elapsed_values = iter([120.0, 120.0, 240.0, 240.0])
    monkeypatch.setattr(main, "race_elapsed", lambda row: next(elapsed_values))

    main.record_passage(main.PassageInput(bib="1"))
    assert main.list_races()[0]["laps"] == 1
    main.record_passage(main.PassageInput(bib="1"))

    assert main.list_races()[0]["laps"] == 5


def test_target_duration_recalculates_after_passage_correction(
    isolated_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    race_id = create_race("Corrected target race", laps=None, target_minutes=10)
    main.setup_participants(race_id, main.ParticipantSetupInput(bibs="12 13"))
    set_race_running(race_id)
    def elapsed(row: object) -> float:
        with main.connect() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM passages WHERE race_id = ? AND cancelled = 0",
                (race_id,),
            ).fetchone()[0]
        return {0: 120.0, 1: 240.0}.get(count, 300.0)

    monkeypatch.setattr(main, "race_elapsed", elapsed)

    main.record_passage(main.PassageInput(bib="12"))
    second = main.record_passage(main.PassageInput(bib="12"))
    assert main.list_races()[0]["laps"] == 5

    main.update_passage_bib(second["id"], main.PassageBibUpdateInput(bib="13"))
    assert main.list_races()[0]["laps"] == 1

    main.record_passage(main.PassageInput(bib="13"))
    assert main.list_races()[0]["laps"] == 4


def test_standings_sort_by_laps_then_total_time(
    isolated_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    race_id = create_race(distance=2)
    main.setup_participants(race_id, main.ParticipantSetupInput(bibs="1 2"))
    set_race_running(race_id)
    elapsed_values = iter([10.0, 20.0, 30.0, 30.0])
    monkeypatch.setattr(main, "race_elapsed", lambda row: next(elapsed_values))

    main.record_passage(main.PassageInput(bib="1"))
    main.record_passage(main.PassageInput(bib="2"))
    main.record_passage(main.PassageInput(bib="1"))

    standings = main.list_races()[0]["standings"]
    assert [item["bib"] for item in standings] == ["1", "2"]
    assert standings[0]["laps_done"] == 2
    assert standings[0]["average_speed_kmh"] == pytest.approx(480.0)


def test_race_details_can_be_edited_and_deleted(isolated_database: None) -> None:
    race_id = create_race("Original", laps=5, distance=1)

    main.update_race_details(
        race_id,
        main.RaceUpdateInput(
            name="Updated",
            laps=12,
            target_minutes=None,
            lap_distance_km=None,
        ),
    )
    race = main.list_races()[0]
    assert race["name"] == "Updated"
    assert race["laps"] == 12
    assert race["lap_distance_km"] is None

    main.delete_race(race_id)
    assert main.list_races() == []


def test_participants_with_passages_cannot_be_removed(
    isolated_database: None,
) -> None:
    race_id = create_race()
    main.setup_participants(race_id, main.ParticipantSetupInput(bibs="1 2"))
    set_race_running(race_id)
    main.record_passage(main.PassageInput(bib="1"))

    with pytest.raises(main.HTTPException, match="déjà enregistrés"):
        main.setup_participants(
            race_id,
            main.ParticipantSetupInput(bibs="2", exclusions="1"),
        )


def test_frontend_escapes_user_controlled_values(isolated_database: None) -> None:
    html = main.index()

    assert "const escapeHtml=" in html
    assert "body{margin:0;min-height:100vh;display:flex;flex-direction:column}" in html
    assert ".layout{flex:1;width:100%}" in html
    assert "Créé par Cedric Grandvaux" in html
    assert "cedric.grandvaux@gmail.com" in html
    assert 'src="/logo-ffc.png"' in html
    assert "tours calculés après 2 tours" in html
    assert "r.laps>1?`${r.laps} tours`" in html
    assert "<th>Tours restants</th>" in html
    assert "<th>Temps total</th>" in html
    assert "x.finished?fmt(x.total_seconds):'—'" in html
    assert 'id="sync-controls"' in html
    assert "races.filter(r=>r.status==='ready').length>=2" in html
    assert "function raceCard(r,showSelection)" in html
    assert "showSelection&&r.status==='ready'" in html
    assert "races.map(r=>raceCard(r,showSelection))" in html
    assert "async function bulkPassage(id)" in html
    assert "onclick=\"bulkPassage(${r.id})\"" in html
    assert "r.status==='running'?`<button onclick=\"timer(${r.id},'pause')\">Pause</button><button onclick=\"bulkPassage(${r.id})\">Passage du peloton</button>`" in html
    assert "escapeHtml(r.name)" in html
    assert "escapeHtml(x.race_name)" in html
    assert "escapeHtml(x.bib)" in html
    assert "escapeHtml(x.bib)" in html
