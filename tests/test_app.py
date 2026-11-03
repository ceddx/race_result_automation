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


def test_unknown_or_finished_bibs_are_rejected(isolated_database: None) -> None:
    race_id = create_race(laps=1)
    main.setup_participants(race_id, main.ParticipantSetupInput(bibs="7"))
    set_race_running(race_id)

    with pytest.raises(main.HTTPException, match="Dossard inconnu"):
        main.record_passage(main.PassageInput(bib="99"))

    main.record_passage(main.PassageInput(bib="7"))
    with pytest.raises(main.HTTPException, match="déjà terminé"):
        main.record_passage(main.PassageInput(bib="7"))


def test_target_duration_calculates_laps_from_first_passage(
    isolated_database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    race_id = create_race("Target race", laps=None, target_minutes=10)
    main.setup_participants(race_id, main.ParticipantSetupInput(bibs="1 2"))
    set_race_running(race_id)
    monkeypatch.setattr(main, "race_elapsed", lambda row: 120.0)

    main.record_passage(main.PassageInput(bib="1"))

    assert main.list_races()[0]["laps"] == 5


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
        main.setup_participants(race_id, main.ParticipantSetupInput(bibs="2"))


def test_frontend_escapes_user_controlled_values(isolated_database: None) -> None:
    html = main.index()

    assert "const escapeHtml=" in html
    assert "escapeHtml(r.name)" in html
    assert "escapeHtml(x.race_name)" in html
    assert "escapeHtml(x.bib)" in html
    assert "r.bibs.map(escapeHtml)" in html
    assert "r.exclusions.map(escapeHtml)" in html
