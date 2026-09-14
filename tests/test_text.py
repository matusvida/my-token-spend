import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import text


def test_a_double_encoded_name_is_read_back_as_utf_8():
    assert text.repair_mojibake("MatÃºÅ¡ Vida") == "Matúš Vida"


def test_a_clean_accented_string_is_left_alone():
    assert text.repair_mojibake("Matúš Vida") == "Matúš Vida"
    assert text.repair_mojibake("café na náměstí") == "café na náměstí"


def test_plain_text_and_empty_values_pass_through():
    assert text.repair_mojibake("acting for the orchestrator") == "acting for the orchestrator"
    assert text.repair_mojibake("") == ""
    assert text.repair_mojibake(None) is None


def test_an_em_dash_written_through_cp1252_is_repaired():
    assert text.repair_mojibake("one â€” two") == "one — two"
