"""Tag parsing, and the value-set semantics the whole classifier depends on."""

from __future__ import annotations

import pytest

from pl.tags import MorphTag, UnknownTagError, parse


def test_single_valued_positions_parse():
    tag = parse("subst:sg:gen:m3")
    assert tag.pos == "subst"
    assert tag.number == {"sg"}
    assert tag.case == {"gen"}
    assert tag.gender == {"m3"}


def test_dotted_positions_are_value_sets():
    """Syncretism is collapsed into the tag, not expanded into interpretations."""
    tag = parse("subst:sg:gen.acc:m2")
    assert tag.case == {"gen", "acc"}


def test_every_position_can_be_a_set():
    tag = parse("subst:sg.pl:nom.gen.dat.acc.inst.loc.voc:n:ncol")
    assert tag.number == {"sg", "pl"}
    assert len(tag.case) == 7
    assert tag.values("collectivity") == {"ncol"}


def test_syncretic_tag_matches_the_case_it_contains():
    """The masculine animate accusative is only diagnosable under intersection.

    `kota` carries one nominal reading, subst:sg:gen.acc:m2. Comparing tags for
    equality would report the correct answer to the course's first hard concept
    as a case error.
    """
    assert parse("subst:sg:gen.acc:m2").matches(parse("subst:sg:acc:m2"))


def test_disjoint_values_do_not_match():
    assert not parse("subst:sg:nom:m2").matches(parse("subst:sg:gen.acc:m2"))


def test_different_pos_never_matches():
    assert not parse("adjp:gen").matches(parse("adj:sg:gen:m3:pos"))


def test_absent_attribute_agrees_vacuously():
    """An infinitive has no case; comparing it on case must not invent a difference."""
    assert parse("inf:imperf").agrees_on("case", parse("subst:sg:gen:m3"))


def test_differing_attrs_reports_only_disjoint_shared_attributes():
    observed = parse("subst:pl:nom.acc.voc:m2")
    expected = parse("subst:sg:gen.acc:m2")
    assert observed.differing_attrs(expected) == ("number",)


def test_optional_trailing_position_is_accepted():
    assert parse("subst:sg:nom:n:ncol").values("collectivity") == {"ncol"}
    assert parse("subst:sg:nom:f").values("collectivity") == frozenset()


def test_unknown_pos_raises_rather_than_yielding_a_null_feature():
    with pytest.raises(UnknownTagError, match="unknown part of speech"):
        parse("wibble:sg:nom")


def test_too_many_positions_raises():
    with pytest.raises(UnknownTagError, match="attribute position"):
        parse("subst:sg:nom:m3:ncol:extra")


def test_ign_is_recognised_as_unknown():
    assert parse("ign").is_unknown
    assert not parse("subst:sg:nom:f").is_unknown


def test_tag_is_hashable_and_frozen():
    assert len({parse("subst:sg:nom:f"), parse("subst:sg:nom:f")}) == 1
    with pytest.raises(AttributeError):
        parse("subst:sg:nom:f").pos = "adj"  # type: ignore[misc]


def test_raw_is_excluded_from_equality():
    """Two tags with the same features compare equal regardless of spelling."""
    a = MorphTag(pos="ign", features=(), raw="ign")
    b = MorphTag(pos="ign", features=(), raw="")
    assert a == b
