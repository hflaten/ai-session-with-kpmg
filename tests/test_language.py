import pytest

from app.agent import service
from app.agent.language import detect_language


@pytest.mark.parametrize("text, language", [
    ("What should we order for Christmas?", "English"),
    ("Hva bør vi bestille til jul?", "Norwegian"),
    ("Hvor mye svinn har vi?", "Norwegian"),            # no æøå, but Norwegian words
    ("Hvilke varer blir utsolgt?", "Norwegian"),
    ("Is Røkt laks running out?", "Norwegian"),          # ø -> Norwegian (simple rule, documented)
    ("Order plan for L01 please", "English"),
])
def test_detect_language(text, language):
    assert detect_language(text) == language


def test_language_goes_into_the_system_prompt(monkeypatch):
    monkeypatch.setattr(service, "describe_schema", lambda: "")
    monkeypatch.setattr(service, "describe_documents", lambda: "")
    assert service.build_system_prompt("p", language="Norwegian").endswith("Answer in Norwegian.")
