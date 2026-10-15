"""Human relevance labels: the record, its JSONL store and the labelling tool."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from evals.cases import CandidateRecord, FilterCase
from evals.labels import (
    Label,
    append_label,
    label_items,
    read_labels,
    run_labelling,
)

_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def _case(case_id: str, profile: str, *ids: str, source: str = "arxiv") -> FilterCase:
    return FilterCase(
        case_id=case_id,
        profile=profile,
        candidates=tuple(
            CandidateRecord(i, f"Title {i}", f"Abstract {i}") for i in ids
        ),
        source=source,
    )


class TestLabel:
    def test_tier_outside_scale_rejected(self) -> None:
        with pytest.raises(ValueError, match="tier"):
            Label(profile="p", item_id="x", tier=4)

    def test_boolean_tier_rejected(self) -> None:
        with pytest.raises(ValueError, match="tier"):
            Label(profile="p", item_id="x", tier=True)

    def test_missing_identity_rejected(self) -> None:
        with pytest.raises(ValueError, match="profile"):
            Label(profile="", item_id="x", tier=1)
        with pytest.raises(ValueError, match="item_id"):
            Label(profile="p", item_id="", tier=1)


class TestLabelStore:
    def test_missing_file_reads_as_no_labels(self, tmp_path: Path) -> None:
        assert read_labels(tmp_path / "labels.jsonl") == {}

    def test_append_then_read_with_later_line_winning(self, tmp_path: Path) -> None:
        path = tmp_path / "labels.jsonl"
        append_label(path, Label("p", "x", 1, note="first"))
        append_label(path, Label("p", "y", 0))
        append_label(path, Label("p", "x", 3, note="changed my mind"))

        labels = read_labels(path)

        assert labels[("p", "x")] == Label("p", "x", 3, note="changed my mind")
        assert labels[("p", "y")].tier == 0
        assert len(path.read_text().splitlines()) == 3

    def test_same_item_under_two_profiles_is_two_labels(self, tmp_path: Path) -> None:
        path = tmp_path / "labels.jsonl"
        append_label(path, Label("a", "x", 0))
        append_label(path, Label("b", "x", 2))

        assert {k: v.tier for k, v in read_labels(path).items()} == {
            ("a", "x"): 0,
            ("b", "x"): 2,
        }

    def test_malformed_line_names_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "labels.jsonl"
        append_label(path, Label("p", "x", 1))
        with path.open("a") as fh:
            fh.write('{"profile": "p", "item_id": "y", "tier": 9}\n')

        with pytest.raises(ValueError, match=r"labels\.jsonl:2"):
            read_labels(path)

    def test_torn_write_is_skipped_and_never_fuses_with_the_next_label(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "labels.jsonl"
        append_label(path, Label("p", "x", 1))
        with path.open("a") as fh:
            fh.write('{"profile": "p", "item_')  # crash mid-write

        append_label(path, Label("p", "y", 2))
        labels = read_labels(path)

        assert {k: v.tier for k, v in labels.items()} == {("p", "x"): 1, ("p", "y"): 2}
        assert "labels.jsonl:2: skipping torn line" in caplog.text


class TestLabelItems:
    def test_dataset_order_deduplicated_per_profile(self) -> None:
        cases = [
            _case("c1", "a", "x", "y"),
            _case("c2", "a", "y", "z"),
            _case("c3", "b", "x", source="rss"),
        ]

        items = label_items(cases)

        assert [(i.profile, i.candidate.id, i.source) for i in items] == [
            ("a", "x", "arxiv"),
            ("a", "y", "arxiv"),
            ("a", "z", "arxiv"),
            ("b", "x", "rss"),
        ]

    def test_profile_subset(self) -> None:
        cases = [_case("c1", "a", "x"), _case("c2", "b", "y")]

        assert [i.profile for i in label_items(cases, profiles=("b",))] == ["b"]


class _Console:
    """Scripted stand-in for ``input``/``print``."""

    def __init__(self, answers: list[str]) -> None:
        self._answers: Iterator[str] = iter(answers)
        self.out: list[str] = []

    def read(self, prompt: str) -> str:
        self.out.append(prompt)
        try:
            return next(self._answers)
        except StopIteration:
            raise EOFError from None

    def write(self, text: str = "") -> None:
        self.out.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.out)


def _session(tmp_path: Path, items, answers: list[str], **kw):
    console = _Console(answers)
    result = run_labelling(
        items,
        labels_path=tmp_path / "labels.jsonl",
        descriptions={"a": "Agents and tool use.", "b": "Robotics."},
        labeller="dave",
        read=console.read,
        write=console.write,
        now=lambda: _NOW,
        **kw,
    )
    return result, console, read_labels(tmp_path / "labels.jsonl")


class TestRunLabelling:
    _items = label_items([_case("c1", "a", "x", "y"), _case("c2", "b", "z")])

    def test_labels_each_item_with_optional_note(self, tmp_path: Path) -> None:
        result, _console, labels = _session(
            tmp_path, self._items, ["1", "0 off-topic hardware", "3"]
        )

        assert labels[("a", "x")] == Label(
            "a", "x", 1, title="Title x", labeller="dave", labelled_at=_NOW.isoformat()
        )
        assert labels[("a", "y")].note == "off-topic hardware"
        assert labels[("b", "z")].tier == 3
        assert (result.labelled_now, result.labelled_total, result.items) == (3, 3, 3)

    def test_shows_item_and_profile_but_never_model_scores(
        self, tmp_path: Path
    ) -> None:
        _result, console, _labels = _session(tmp_path, self._items, ["q"])

        assert "Agents and tool use." in console.text
        assert "Title x" in console.text and "Abstract x" in console.text
        assert "https://arxiv.org/abs/x" in console.text
        assert "0 skip" in console.text and "3 deep extract" in console.text
        assert "score" not in console.text.lower()

    def test_skip_back_and_relabel(self, tmp_path: Path) -> None:
        result, _console, labels = _session(
            tmp_path, self._items, ["s", "2", "b", "0", "1", "q"]
        )

        assert ("a", "x") not in labels
        assert labels[("a", "y")].tier == 0  # relabelled after going back
        assert labels[("b", "z")].tier == 1
        assert result.labelled_now == 3

    def test_invalid_answer_reprompts(self, tmp_path: Path) -> None:
        _result, console, labels = _session(
            tmp_path, self._items, ["7", "maybe", "\u00b2", "2"]
        )

        assert labels[("a", "x")].tier == 2
        assert console.text.count("not a label") == 3

    def test_skips_are_counted_in_the_closing_line(self, tmp_path: Path) -> None:
        _result, console, _labels = _session(tmp_path, self._items, ["s", "1", "q"])

        assert "1 labelled this session (1 skipped; offered again next time)" in (
            console.text
        )

    def test_profile_missing_from_config_is_flagged(self, tmp_path: Path) -> None:
        items = label_items([_case("c9", "gone", "x")])

        _result, console, _labels = _session(tmp_path, items, ["q"])

        assert "not in this config" in console.text

    def test_resume_skips_labelled_items(self, tmp_path: Path) -> None:
        append_label(tmp_path / "labels.jsonl", Label("a", "x", 1))

        result, console, labels = _session(tmp_path, self._items, ["0", "0"])

        assert "Title x" not in console.text
        assert labels[("a", "x")].tier == 1
        assert (result.labelled_now, result.labelled_total) == (2, 3)

    def test_relabel_revisits_labelled_items_showing_current_label(
        self, tmp_path: Path
    ) -> None:
        append_label(tmp_path / "labels.jsonl", Label("a", "x", 1))

        _result, console, labels = _session(tmp_path, self._items, ["3"], relabel=True)

        assert "current label: 1 ingest" in console.text
        assert labels[("a", "x")].tier == 3

    def test_end_of_input_keeps_what_was_labelled(self, tmp_path: Path) -> None:
        result, _console, labels = _session(tmp_path, self._items, ["2"])

        assert list(labels) == [("a", "x")]
        assert result.labelled_now == 1

    def test_empty_abstract_is_called_out(self, tmp_path: Path) -> None:
        items = label_items(
            [
                FilterCase(
                    "c", "b", (CandidateRecord("https://blog/post", "Post", ""),), "rss"
                )
            ]
        )

        _result, console, _labels = _session(tmp_path, items, ["q"])

        assert "(no abstract)" in console.text
        assert "https://blog/post" in console.text
