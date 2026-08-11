"""`DestinationChoice` (Phase 6): the user's choice, persisted per note.

Distinct from `Destination` (the already-resolved target) — see `models.py`
and `PLANO_PAGINA_MAE.md` §4.

Also covers `NoteDraft` normalization and validation (title_slug, tag
normalization, leading-H1 stripping, empty body rejection). These cases
were moved here from `test_pipeline.py` (D12), which had an empty
"# validação do draft" header — this is probably where they should have
lived from the start, instead of sitting under "# retry de transporte".
"""

import pytest

from notemcp.models import DestinationChoice, DestinationKind, NoteDraft, title_slug


def test_database_factory_has_no_page_id():
    choice = DestinationChoice.database()
    assert choice.kind is DestinationKind.DATABASE
    assert choice.page_id is None


def test_page_factory_defaults_to_root():
    choice = DestinationChoice.page()
    assert choice.kind is DestinationKind.PAGE
    assert choice.page_id is None
    assert choice.is_root_page


def test_page_factory_with_id_is_not_root():
    choice = DestinationChoice.page("abc123")
    assert choice.page_id == "abc123"
    assert not choice.is_root_page


def test_empty_page_id_is_rejected_instead_of_falling_back_to_the_root():
    """`page_id == ""` must not silently become `or root` (risk #2)."""
    with pytest.raises(ValueError):
        DestinationChoice(kind=DestinationKind.PAGE, page_id="")


def test_whitespace_only_page_id_is_rejected():
    with pytest.raises(ValueError):
        DestinationChoice(kind=DestinationKind.PAGE, page_id="   ")


def test_database_with_a_page_id_is_rejected():
    with pytest.raises(ValueError):
        DestinationChoice(kind=DestinationKind.DATABASE, page_id="x")


def test_destination_choice_is_frozen_and_hashable():
    choice = DestinationChoice.page("x")
    with pytest.raises(Exception):
        choice.page_id = "y"
    assert hash(choice) == hash(DestinationChoice.page("x"))


def test_title_slug_ignores_accent_case_and_punctuation():
    assert title_slug("Cálculo I!") == title_slug("CALCULO i")


def test_tags_are_normalised_and_capped():
    draft = NoteDraft(
        title="t", doc_type="nota",
        tags=[" Backend ", "backend", "a,b", "", "x", "y", "z", "w"],
        summary="s", body_md="corpo",
    )
    assert draft.tags == ["backend", "a b", "x", "y", "z"]


def test_leading_h1_repeating_the_title_is_dropped():
    """Small models repeat the title as a heading; it must not reach
    Notion."""
    draft = NoteDraft(
        title="Reunião de Planejamento",
        doc_type="reuniao", tags=[], summary="s",
        body_md="# reuniao de planejamento!\n\nconteúdo real",
    )
    assert draft.body_md == "conteúdo real"


def test_leading_h1_with_different_text_is_kept():
    draft = NoteDraft(
        title="Reunião de Planejamento",
        doc_type="reuniao", tags=[], summary="s",
        body_md="# Contexto\n\nconteúdo real",
    )
    assert draft.body_md.startswith("# Contexto")


def test_empty_body_is_rejected():
    with pytest.raises(ValueError):
        NoteDraft(title="t", doc_type="nota", tags=[], summary="s", body_md="   ")
