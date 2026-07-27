import json

from hermes_state import SessionDB
from tools.conversation_group_tool import conversation_group


def test_assign_and_remove_companion_group(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1", source="whatsapp")
    db.set_session_title("s1", "Planejar hunt")

    assigned = json.loads(conversation_group(
        action="assign", session_ids=["s1"], group="🎮 Tibia", db=db
    ))
    assert assigned["changed_session_ids"] == ["s1"]
    assert db.get_session("s1")["title"] == "N / 🎮 Tibia · Planejar hunt"

    removed = json.loads(conversation_group(action="remove", session_ids=["s1"], db=db))
    assert removed["changed_session_ids"] == ["s1"]
    assert db.get_session("s1")["title"] == "Planejar hunt"


def test_group_assignment_reports_missing_exact_ids(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    result = json.loads(conversation_group(
        action="assign", session_ids=["missing"], group="Aura", db=db
    ))
    assert result["success"] is False
    assert result["missing_session_ids"] == ["missing"]
