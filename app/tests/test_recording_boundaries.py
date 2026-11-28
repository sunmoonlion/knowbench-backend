import pytest
from drivers import workbench_tape_driver as driver
from preview_samples import Company


def test_failed_command_or_turn_never_replaces_a_known_recording(tmp_path):
    out = tmp_path / "tape.json"
    out.write_text("previous")
    completed = {"type": "turn/completed", "payload": {"turn": {"status": "completed"}}}
    assert driver.turn_outcome([completed]) == "completed"
    assert driver.turn_outcome([completed, {"type": "command/failed"}]) == "failed"
    assert driver.turn_outcome([]) is None
    assert (
        driver.turn_outcome(
            [{"type": "turn/completed", "payload": {"turn": {"status": "interrupted"}}}]
        )
        == "failed"
    )
    for status in ["failed", "timeout", "completed"]:
        wrote = driver.store_recording(
            out, "new", {"sample": {"turns": [{"outcome": status}]}}
        )
        assert wrote == (status == "completed")
        assert out.read_text() == ("new" if wrote else "previous")


async def test_existing_recording_directory_does_not_allocate_database(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ROOT", str(tmp_path))
    monkeypatch.setenv("APP_SERVER_TOKEN", "fixture")
    monkeypatch.setenv("AGENT_TEST_DATABASE_URL", "unused")
    monkeypatch.setenv("TAPE_OUT", str(tmp_path / "archive.json"))
    monkeypatch.setattr(
        driver, "create_engine", lambda *a: pytest.fail("database allocated")
    )
    project = tmp_path / driver.PROJECT
    project.mkdir()
    marker = project / "already-there"
    marker.touch()
    assert await driver.main() == 3
    assert marker.exists()


@pytest.mark.parametrize(
    ("growth", "observation"),
    [
        ((-0.1, 0.1), "并非逐年同向变化"),
        ((0.0, 0.0), "保持不变"),
        ((-0.1, -0.1), "逐年下降"),
        ((0.1, 0.1), "逐年增加"),
    ],
)
def test_preview_observations_match_the_displayed_figures(growth, observation):
    sample = Company(
        "示例",
        "sample",
        "local",
        revenue=1000,
        growth=growth,
        gross=(0.4, 0.3, 0.2),
        net=(0.1, 0.1, 0.1),
        leverage=0.2,
    )
    notes = sample.note()["observations"]
    assert observation in notes[0]
    assert "40.0%" in notes[1] and "20.0%" in notes[1]
