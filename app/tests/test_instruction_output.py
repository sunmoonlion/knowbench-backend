import pytest

from app.application.workbench.instruction_turn import (
    InstructionOutputError,
    instruction_output,
)


def test_parts_make_one_complete_object():
    assert instruction_output(['{"result":{"s', 'tatus":"已核对"}}']) == {
        "result": {"status": "已核对"}
    }


@pytest.mark.parametrize(
    ("parts", "code"),
    [
        ([], "instruction_final_missing"),
        (["null"], "instruction_output_not_object"),
        (['{"row":{"status":1,', '"status":2}}'], "instruction_final_invalid_json"),
        (["{}", "{}"], "instruction_final_invalid_json"),
        (["作者私有资料", "{}"], "instruction_final_invalid_json"),
        (["{}", None], "instruction_final_invalid"),
    ],
)
def test_invalid_final_returns_only_a_bounded_code(parts, code):
    with pytest.raises(InstructionOutputError) as exc:
        instruction_output(parts)
    assert str(exc.value) == code
