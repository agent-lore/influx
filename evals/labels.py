"""Human relevance labels for filter candidates, and the tool that collects them.

Without labels the filter can only be measured against another model's
scores.  A label is the operator's own call on one candidate for one
profile, on the four-step scale the pipeline acts on:

    0 skip          not relevant; should not be ingested
    1 ingest        relevant: worth a note with a Tier-1 summary
    2 full text     worth fetching and storing the full text (Tier 2)
    3 deep extract  worth a deep Tier-3 extraction

A model's score maps onto the same scale through the profile's
``relevance`` / ``full_text`` / ``deep_extract`` thresholds, so one label
grades the model at every gate (see :func:`evals.metrics.grade_filter`).

Labels are appended to ``labels.jsonl`` in the dataset directory, one line
per decision.  A later line for the same ``(profile, item_id)`` wins, so the
file is resumable and relabelling never rewrites history.  A line cut short
by a crash mid-write is skipped with a warning rather than blocking every
later read.
"""

from __future__ import annotations

import json
import logging
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from evals.cases import CandidateRecord, FilterCase

_log = logging.getLogger(__name__)

LABELS_FILE = "labels.jsonl"
TIER_NAMES: tuple[str, ...] = ("skip", "ingest", "full text", "deep extract")
MAX_TIER = len(TIER_NAMES) - 1

LabelKey = tuple[str, str]
"""``(profile, item_id)``: relevance is a property of an item *for a profile*."""

_WRAP = 88
_LEGEND = "  ".join(f"{n} {name}" for n, name in enumerate(TIER_NAMES))
_PROMPT = (
    f"label [0-{MAX_TIER} + optional note, s=skip, b=back, p=profile, ?=help, q=quit]> "
)
_HELP = f"""\
Label each candidate for the profile shown, on the pipeline's own scale:
  0 skip          not relevant to this profile; it should not be ingested
  1 ingest        relevant: worth a note with a short summary
  2 full text     clearly relevant: worth fetching the full text
  3 deep extract  core to the profile: worth a deep claims/datasets extraction
Type a digit, optionally followed by a note ("0 hardware, not agents").
Judge the paper itself; open the link when the abstract is too thin to tell.
  s  skip this item for now      b  go back one item
  p  show the profile again      q  quit (labels are already saved)
Labels go to {LABELS_FILE}; the latest label for an item wins."""


@dataclass(frozen=True, slots=True)
class Label:
    """One human relevance decision.  ``title`` is kept for readability only."""

    profile: str
    item_id: str
    tier: int
    title: str = ""
    note: str = ""
    labeller: str = ""
    labelled_at: str = ""

    def __post_init__(self) -> None:
        if not self.profile:
            raise ValueError("label needs a profile")
        if not self.item_id:
            raise ValueError("label needs an item_id")
        valid = isinstance(self.tier, int) and not isinstance(self.tier, bool)
        if not valid or not 0 <= self.tier <= MAX_TIER:
            raise ValueError(f"label tier must be 0-{MAX_TIER}, got {self.tier!r}")

    @property
    def key(self) -> LabelKey:
        return (self.profile, self.item_id)


def read_labels(path: Path) -> dict[LabelKey, Label]:
    """Every label in *path*, the latest line winning per ``(profile, item)``.

    A line that is not JSON at all can only be a write torn by a crash: it
    is skipped with a warning.  A JSON line that is not a valid label (a
    hand edit gone wrong) raises :class:`ValueError` naming the line, rather
    than grading against a half-read file.
    """
    if not path.exists():
        return {}
    labels: dict[LabelKey, Label] = {}
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                _log.warning("%s:%d: skipping torn line", path.name, lineno)
                continue
            try:
                label = Label(**row)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path.name}:{lineno}: bad label: {exc}") from exc
            labels[label.key] = label
    return labels


def _ends_mid_line(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open("rb") as fh:
        fh.seek(-1, 2)
        return fh.read(1) != b"\n"


def append_label(path: Path, label: Label) -> None:
    """Append *label* as one line, first closing any line a crash left open
    so the new label never fuses with a torn one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lead = "\n" if _ends_mid_line(path) else ""
    with path.open("a", encoding="utf-8") as fh:
        fh.write(lead + json.dumps(asdict(label), ensure_ascii=False) + "\n")


def label_tiers(labels: Mapping[LabelKey, Label]) -> dict[LabelKey, int]:
    """The ``key → tier`` view the metrics take."""
    return {key: label.tier for key, label in labels.items()}


# ── what to label ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LabelItem:
    """One candidate to label, with the profile it was fetched for."""

    profile: str
    source: str
    candidate: CandidateRecord

    @property
    def key(self) -> LabelKey:
        return (self.profile, self.candidate.id)


def label_items(
    cases: Sequence[FilterCase], *, profiles: Sequence[str] = ()
) -> list[LabelItem]:
    """Every distinct ``(profile, candidate)`` in *cases*, in dataset order.

    Dataset order keeps one profile's candidates together, so the labeller
    holds one interest profile in mind at a time.
    """
    wanted = set(profiles)
    seen: set[LabelKey] = set()
    out: list[LabelItem] = []
    for case in cases:
        if wanted and case.profile not in wanted:
            continue
        for cand in case.candidates:
            item = LabelItem(case.profile, case.source, cand)
            if item.key not in seen:
                seen.add(item.key)
                out.append(item)
    return out


# ── the interactive tool ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LabellingResult:
    """How a labelling session ended."""

    items: int
    labelled_now: int
    labelled_total: int


def _link(item: LabelItem) -> str:
    if item.candidate.id.startswith(("http://", "https://")):
        return item.candidate.id
    return f"https://arxiv.org/abs/{item.candidate.id}"


def _wrap(text: str) -> str:
    return "\n".join(
        textwrap.fill(para.strip(), _WRAP) for para in text.splitlines() if para.strip()
    )


_TIER_KEYS = {str(n): n for n in range(MAX_TIER + 1)}


def _parse_tier(answer: str) -> tuple[int, str] | None:
    head, _, note = answer.partition(" ")
    tier = _TIER_KEYS.get(head)
    return None if tier is None else (tier, note.strip())


class _Session:
    """State for one run of :func:`run_labelling`; not reused across runs."""

    def __init__(
        self,
        queue: Sequence[LabelItem],
        *,
        labels_path: Path,
        labels: dict[LabelKey, Label],
        descriptions: Mapping[str, str],
        labeller: str,
        write: Callable[[str], None],
        now: Callable[[], datetime],
    ) -> None:
        self.queue = queue
        self.labels_path = labels_path
        self.labels = labels
        self.descriptions = descriptions
        self.labeller = labeller
        self.write = write
        self.now = now
        self.labelled_now = 0
        self.skipped = 0
        self.shown_profile: str | None = None

    def show_profile(self, profile: str) -> None:
        self.write("")
        self.write(f"=== profile: {profile} ===")
        description = self.descriptions.get(profile, "")
        self.write(
            _wrap(description)
            or "(not in this config, so no description: check --config)"
        )
        self.write(_LEGEND)
        self.shown_profile = profile

    def show_item(self, pos: int) -> None:
        item = self.queue[pos]
        if item.profile != self.shown_profile:
            self.show_profile(item.profile)
        cand = item.candidate
        self.write("─" * _WRAP)
        self.write(f"[{pos + 1}/{len(self.queue)}] {item.profile} · {item.source}")
        self.write(_wrap(cand.title) or "(no title)")
        self.write(_link(item))
        self.write("")
        self.write(_wrap(cand.abstract) or "(no abstract)")
        current = self.labels.get(item.key)
        if current is not None:
            self.write(f"current label: {current.tier} {TIER_NAMES[current.tier]}")

    def record(self, pos: int, tier: int, note: str) -> None:
        item = self.queue[pos]
        label = Label(
            profile=item.profile,
            item_id=item.candidate.id,
            tier=tier,
            title=item.candidate.title,
            note=note,
            labeller=self.labeller,
            labelled_at=self.now().isoformat(),
        )
        append_label(self.labels_path, label)
        self.labels = {**self.labels, item.key: label}
        self.labelled_now += 1


def run_labelling(
    items: Sequence[LabelItem],
    *,
    labels_path: Path,
    descriptions: Mapping[str, str],
    labeller: str,
    relabel: bool = False,
    read: Callable[[str], str] | None = None,
    write: Callable[[str], None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> LabellingResult:
    """Walk the labeller through *items*, appending each decision at once.

    Items already labelled are skipped unless *relabel*.  Model scores are
    never shown: the point is an independent key.  Quitting, end of input
    or Ctrl-C all stop cleanly, since every label is written as it is made.
    *read* / *write* / *now* default to ``input`` / ``print`` / the UTC
    clock, looked up at call time.
    """
    read = read or input
    write = write or print
    now = now or (lambda: datetime.now(UTC))
    labels = read_labels(labels_path)
    queue = [i for i in items if relabel or i.key not in labels]
    keys = {i.key for i in items}
    write(
        f"{sum(1 for k in keys if k in labels)}/{len(keys)} candidates labelled; "
        f"{len(queue)} to go.  Type ? for help."
    )
    session = _Session(
        queue,
        labels_path=labels_path,
        labels=labels,
        descriptions=descriptions,
        labeller=labeller,
        write=write,
        now=now,
    )
    pos = 0
    shown = -1
    while pos < len(queue):
        if pos != shown:
            session.show_item(pos)
            shown = pos
        try:
            answer = read(_PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            write("")
            break
        parsed = _parse_tier(answer)
        if parsed is not None:
            session.record(pos, *parsed)
            pos += 1
        elif answer == "s":
            session.skipped += 1
            pos += 1
        elif answer == "b":
            pos = max(0, pos - 1)
            shown = -1
        elif answer == "p":
            session.show_profile(queue[pos].profile)
        elif answer in ("?", "h"):
            write(_HELP)
        elif answer == "q":
            break
        else:
            write(f"{answer!r} is not a label; type ? for help.")
    total = sum(1 for k in keys if k in session.labels)
    skipped = (
        f" ({session.skipped} skipped; offered again next time)"
        if session.skipped
        else ""
    )
    write(
        f"{session.labelled_now} labelled this session{skipped}; "
        f"{total}/{len(keys)} done."
    )
    return LabellingResult(
        items=len(keys), labelled_now=session.labelled_now, labelled_total=total
    )
