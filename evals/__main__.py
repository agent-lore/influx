"""``python -m evals`` — build datasets, run models, compare runs.

    uv run python -m evals build-dataset --name oct15 --config prod.toml \\
        --knowledge-dir /path/to/lithos/knowledge
    uv run python -m evals run --dataset oct15 --config prod.toml \\
        --label gpt41mini --model openai/gpt-4.1-mini --provider openrouter
    uv run python -m evals run --dataset oct15 --config prod.toml \\
        --label luna-none --model openai/gpt-6-luna --provider openrouter \\
        --reasoning-effort none
    uv run python -m evals compare --dataset oct15 --config prod.toml \\
        --baseline gpt41mini --candidate luna-none \\
        --judge-model anthropic/claude-sonnet-4.6 --judge-provider openrouter
    uv run python -m evals label --dataset oct15 --config prod.toml
    uv run python -m evals grade --dataset oct15 --config prod.toml \\
        --runs gpt41mini,luna-none

``label`` collects your own relevance calls on the filter candidates
(``datasets/<name>/labels.jsonl``); ``grade`` scores runs against them and
``compare`` adds the same grades whenever labels exist.  See
``docs/operations/model-evals.md``.

API keys are read from the environment (``--env-file`` loads a dotenv file
first).  Datasets, runs and the response cache live under ``--root``
(default: the ``evals/`` directory) and are git-ignored.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from evals.cases import load_dataset, read_results, write_jsonl
from evals.dataset import DatasetSpec, build_dataset
from evals.judge import Judge, summarize_verdicts
from evals.labels import (
    LABELS_FILE,
    label_items,
    label_tiers,
    read_labels,
    run_labelling,
)
from evals.metrics import (
    compare_filter,
    grade_filter,
    judge_label_agreement,
    summarize_run,
    summarize_shape,
)
from evals.report import (
    render_filter_comparison,
    render_judge,
    render_judge_vs_labels,
    render_label_grades,
    render_label_misses,
    render_label_mix,
    render_run_table,
    render_shape,
)
from evals.runner import ModelSpec, Runner
from influx.config import AppConfig, load_config

_ROOT = Path(__file__).resolve().parent
_SLOTS = ("filter", "tier1", "tier3")


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="influx.toml to take prompts, profiles and providers from",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=_ROOT,
        help="where datasets/, runs/ and .cache/ live",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="dotenv file holding provider API keys",
    )
    parser.add_argument("-v", "--verbose", action="store_true")


def _setup(args: argparse.Namespace):
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.env_file:
        load_dotenv(args.env_file)
    return load_config(args.config, check_api_keys=False)


def _gate_thresholds(config: AppConfig) -> dict[str, tuple[int, int, int]]:
    """``profile → (relevance, full_text, deep_extract)``: the label tiers' gates."""
    return {
        p.name: (
            p.thresholds.relevance,
            p.thresholds.full_text,
            p.thresholds.deep_extract,
        )
        for p in config.profiles
    }


def _default_labeller() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # no login name (e.g. a bare container)
        return ""


def _label_tiers(dataset_dir: Path) -> dict[tuple[str, str], int]:
    try:
        return label_tiers(read_labels(dataset_dir / LABELS_FILE))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _split(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def cmd_build_dataset(args: argparse.Namespace) -> int:
    config = _setup(args)
    spec = DatasetSpec(
        name=args.name,
        lookback_days=args.lookback_days,
        batches_per_profile=args.batches_per_profile,
        tier1_per_profile=args.tier1_per_profile,
        tier3_limit=args.tier3_limit,
        seed=args.seed,
        include_rss=not args.no_rss,
        profiles=tuple(args.profiles.split(",")) if args.profiles else (),
    )
    counts = build_dataset(
        config,
        spec,
        out_dir=args.root / "datasets" / args.name,
        knowledge_dir=args.knowledge_dir,
        config_path=args.config,
    )
    print(json.dumps(counts))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = _setup(args)
    dataset_dir = args.root / "datasets" / args.dataset
    filter_cases, tier1_cases, tier3_cases = load_dataset(dataset_dir)
    spec = ModelSpec(
        label=args.label,
        model=args.model,
        provider=args.provider,
        reasoning_effort=args.reasoning_effort,
        temperature=args.temperature,
        json_schema_strict=args.strict,
        max_tokens=args.max_tokens,
        request_timeout=args.timeout,
    )
    runner = Runner(
        config, spec, cache_dir=args.root / ".cache", concurrency=args.concurrency
    )
    run_dir = args.root / "runs" / args.dataset / args.label
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "spec.json").write_text(json.dumps(asdict(spec), indent=2))
    slots = args.slots.split(",")
    limit = args.limit
    summaries = []
    if "filter" in slots and filter_cases:
        results = runner.run_filter(filter_cases[:limit])
        write_jsonl(run_dir / "filter.jsonl", results)
        summaries.append(summarize_run(results))
    if "tier1" in slots and tier1_cases:
        results = runner.run_tier1(tier1_cases[:limit])
        write_jsonl(run_dir / "tier1.jsonl", results)
        summaries.append(summarize_run(results))
    if "tier3" in slots and tier3_cases:
        results = runner.run_tier3(tier3_cases[:limit])
        write_jsonl(run_dir / "tier3.jsonl", results)
        summaries.append(summarize_run(results))
    table = render_run_table(summaries)
    (run_dir / "summary.md").write_text(table + "\n")
    print(table)
    return 0


def _load_run(root: Path, dataset: str, label: str):
    run_dir = root / "runs" / dataset / label
    if not run_dir.exists():
        raise SystemExit(f"run {label!r} not found under {run_dir.parent}")
    return {slot: read_results(run_dir / f"{slot}.jsonl") for slot in _SLOTS}


def cmd_compare(args: argparse.Namespace) -> int:
    config = _setup(args)
    dataset_dir = args.root / "datasets" / args.dataset
    filter_cases, tier1_cases, tier3_cases = load_dataset(dataset_dir)
    base = _load_run(args.root, args.dataset, args.baseline)
    cand = _load_run(args.root, args.dataset, args.candidate)
    profiles = {p.name: p for p in config.profiles}
    relevance = {name: p.thresholds.relevance for name, p in profiles.items()}

    out: list[str] = [
        f"# Eval: {args.candidate} vs {args.baseline} on `{args.dataset}`",
        "",
    ]
    out += ["## Validity, latency and cost", ""]
    out.append(
        render_run_table(
            [summarize_run(r) for r in (*base.values(), *cand.values()) if r]
        )
    )

    if base["filter"] and cand["filter"]:
        thresholds = sorted(
            {
                t
                for p in profiles.values()
                for t in (
                    p.thresholds.relevance,
                    p.thresholds.full_text,
                    p.thresholds.deep_extract,
                )
            }
        )
        cmp = compare_filter(
            filter_cases,
            base["filter"],
            cand["filter"],
            thresholds=thresholds,
            relevance=relevance,
        )
        out += ["", "## Filter: score agreement", "", render_filter_comparison(cmp)]
        out += [
            "",
            "Ingest-decision flips (at each profile's own relevance threshold): "
            f"{len(cmp.disagreements)}",
        ]
        if cmp.disagreements:
            out += [
                "",
                "| case | candidate | baseline | candidate model |",
                "|---|---|---:|---:|",
            ]
            out += [
                f"| {c} | {i} | {b} | {d} |" for c, i, b, d in cmp.disagreements[:40]
            ]
    labels = _label_tiers(dataset_dir)
    gates = _gate_thresholds(config)
    if labels and base["filter"] and cand["filter"]:
        grades = [
            (label, grade_filter(filter_cases, run["filter"], labels, gates))
            for label, run in ((args.baseline, base), (args.candidate, cand))
        ]
        out += [
            "",
            "## Filter: graded against your labels",
            "",
            render_label_grades(grades),
        ]
    for slot in ("tier1", "tier3"):
        if base[slot] and cand[slot]:
            out += ["", f"## {slot}: output shape", ""]
            out.append(
                render_shape(summarize_shape(base[slot]), summarize_shape(cand[slot]))
            )

    if args.judge_model:
        out += ["", f"## Judge: {args.judge_model}", ""]
        judge = Judge(
            config,
            model=args.judge_model,
            provider=args.judge_provider,
            reasoning_effort=args.judge_reasoning_effort,
            concurrency=args.concurrency,
            **(
                {"source_char_cap": args.judge_source_cap}
                if args.judge_source_cap is not None
                else {}
            ),
        )
        run_dir = args.root / "runs" / args.dataset / args.candidate
        judge_slots = set(args.judge_slots.split(","))
        for slot, cases in (("tier1", tier1_cases), ("tier3", tier3_cases)):
            if slot not in judge_slots:
                continue
            rows = _judge_rows(
                slot, cases, base[slot], cand[slot], profiles, args.judge_limit
            )
            if not rows:
                continue
            verdicts = judge.judge_outputs(slot, rows)
            write_jsonl(run_dir / f"judge-{slot}-vs-{args.baseline}.jsonl", verdicts)
            out += [f"**{slot}** — " + render_judge(summarize_verdicts(verdicts)), ""]
        if "filter" in judge_slots and base["filter"] and cand["filter"]:
            cmp = compare_filter(
                filter_cases, base["filter"], cand["filter"], relevance=relevance
            )
            rows = _filter_flip_rows(
                filter_cases,
                base["filter"],
                cand["filter"],
                cmp.disagreements,
                profiles,
            )
            if rows:
                verdicts = judge.judge_filter_flips(rows[: args.judge_limit])
                write_jsonl(
                    run_dir / f"judge-filter-vs-{args.baseline}.jsonl", verdicts
                )
                out += [
                    "**filter flips** — " + render_judge(summarize_verdicts(verdicts)),
                    "",
                ]
                if labels:
                    check = judge_label_agreement(
                        [
                            (v.case_id, v.item_id, v.winner)
                            for v in verdicts
                            if not v.error
                        ],
                        cmp.disagreements,
                        {c.case_id: c.profile for c in filter_cases},
                        labels,
                        gates,
                    )
                    out += [render_judge_vs_labels(check), ""]

    text = "\n".join(out) + "\n"
    target = args.out or (
        args.root
        / "runs"
        / args.dataset
        / f"compare-{args.candidate}-vs-{args.baseline}.md"
    )
    target.write_text(text, encoding="utf-8")
    print(text)
    print(f"written: {target}", file=sys.stderr)
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    config = _setup(args)
    dataset_dir = args.root / "datasets" / args.dataset
    filter_cases, _tier1, _tier3 = load_dataset(dataset_dir)
    if not filter_cases:
        raise SystemExit(f"no filter cases in {dataset_dir}; build the dataset first")
    items = label_items(filter_cases, profiles=_split(args.profiles))
    if not items:
        raise SystemExit(f"no candidates for profiles {args.profiles!r}")
    _label_tiers(dataset_dir)  # a bad labels file fails here, not mid-session
    run_labelling(
        items,
        labels_path=dataset_dir / LABELS_FILE,
        descriptions={p.name: p.description for p in config.profiles},
        labeller=args.labeller,
        relabel=args.relabel,
    )
    return 0


def cmd_grade(args: argparse.Namespace) -> int:
    config = _setup(args)
    dataset_dir = args.root / "datasets" / args.dataset
    filter_cases, _tier1, _tier3 = load_dataset(dataset_dir)
    labels = _label_tiers(dataset_dir)
    if not labels:
        raise SystemExit(
            f"no labels in {dataset_dir / LABELS_FILE}; run `evals label` first"
        )
    gates = _gate_thresholds(config)
    grades = []
    for run in _split(args.runs):
        results = _load_run(args.root, args.dataset, run)["filter"]
        if not results:
            raise SystemExit(f"run {run!r} has no filter results to grade")
        grades.append((run, grade_filter(filter_cases, results, labels, gates)))
    in_dataset = [labels[i.key] for i in label_items(filter_cases) if i.key in labels]
    out = [
        f"# Filter grades on `{args.dataset}`",
        "",
        "## Filter: graded against your labels",
        "",
        render_label_mix(in_dataset),
        "",
        render_label_grades(grades),
    ]
    for run, grade in grades:
        out += [
            "",
            f"### {run}: ingest misses",
            "",
            render_label_misses(grade, limit=args.misses),
        ]
    text = "\n".join(out) + "\n"
    target = args.out or args.root / "runs" / args.dataset / "grade.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(text)
    print(f"written: {target}", file=sys.stderr)
    return 0


def _judge_rows(slot, cases, base_results, cand_results, profiles, limit):
    base_by = {r.case_id: r for r in base_results if r.ok}
    cand_by = {r.case_id: r for r in cand_results if r.ok}
    rows = []
    for case in cases:
        b, c = base_by.get(case.case_id), cand_by.get(case.case_id)
        if b is None or c is None or b.output is None or c.output is None:
            continue
        if slot == "tier1":
            profile = profiles.get(case.profile)
            task = (
                "Tier-1 enrichment of a paper abstract: contributions (1-6), method, "
                "result, and relevance to this interest profile:\n"
                + (profile.description if profile else case.profile)
            )
            source = f"Title: {case.title}\n\nAbstract: {case.abstract}"
        else:
            task = (
                "Tier-3 deep extraction from a paper's full text: claims, datasets, "
                "builds_on (with arXiv ids when known), open_questions, "
                "potential_connections."
            )
            source = f"Title: {case.title}\n\n{case.full_text}"
        rows.append((case.case_id, task, source, b.output, c.output))
    return rows[:limit]


def _filter_flip_rows(cases, base_results, cand_results, flips, profiles):
    case_by = {c.case_id: c for c in cases}
    base_by = {r.case_id: r for r in base_results}
    cand_by = {r.case_id: r for r in cand_results}
    rows = []
    for case_id, item_id, _b, _c in flips:
        case = case_by[case_id]
        cand = next((x for x in case.candidates if x.id == item_id), None)
        if cand is None:
            continue
        profile = profiles.get(case.profile)
        description = profile.description if profile else case.profile
        threshold = profile.thresholds.relevance if profile else 7
        rows.append(
            (
                case_id,
                item_id,
                description,
                threshold,
                f"Title: {cand.title}\n\nAbstract: {cand.abstract}",
                _score_row(base_by.get(case_id), item_id),
                _score_row(cand_by.get(case_id), item_id),
            )
        )
    return rows


def _score_row(result, item_id: str) -> dict:
    """The ``{score, reason}`` a filter response gave *item_id* (0 = omitted)."""
    omitted = {"score": 0, "reason": "not returned (below min score)"}
    if result is None or result.output is None:
        return omitted
    for r in result.output.get("results", []):
        if r["id"] == item_id:
            return {"score": r["score"], "reason": r["reason"]}
    return omitted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evals",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "build-dataset", help="snapshot candidates + notes into a dataset"
    )
    _common(p)
    p.add_argument("--name", required=True)
    p.add_argument(
        "--knowledge-dir",
        type=Path,
        default=None,
        help="Lithos knowledge dir to mine Tier-3 cases from",
    )
    p.add_argument("--lookback-days", type=int, default=3)
    p.add_argument("--batches-per-profile", type=int, default=2)
    p.add_argument("--tier1-per-profile", type=int, default=4)
    p.add_argument("--tier3-limit", type=int, default=40)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--no-rss", action="store_true")
    p.add_argument(
        "--profiles", default="", help="comma-separated subset of profile names"
    )
    p.set_defaults(func=cmd_build_dataset)

    p = sub.add_parser("run", help="send a dataset to one model")
    _common(p)
    p.add_argument("--dataset", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--provider", required=True)
    p.add_argument("--reasoning-effort", default=None)
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--max-tokens", type=int, default=None)
    p.add_argument("--timeout", type=int, default=None)
    p.add_argument(
        "--strict",
        dest="strict",
        action="store_true",
        default=None,
        help="force json_schema_strict on",
    )
    p.add_argument(
        "--no-strict",
        dest="strict",
        action="store_false",
        help="force json_schema_strict off",
    )
    p.add_argument("--slots", default=",".join(_SLOTS))
    p.add_argument("--limit", type=int, default=None, help="cases per slot")
    p.add_argument("--concurrency", type=int, default=4)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser(
        "compare", help="compare two runs (optionally with an LLM judge)"
    )
    _common(p)
    p.add_argument("--dataset", required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--judge-model", default=None)
    p.add_argument("--judge-provider", default="openrouter")
    p.add_argument("--judge-reasoning-effort", default=None)
    p.add_argument("--judge-limit", type=int, default=60)
    p.add_argument(
        "--judge-slots",
        default="filter,tier1,tier3",
        help="comma-separated subset of slots to (re-)judge",
    )
    p.add_argument("--judge-source-cap", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--out", type=Path, default=None)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser(
        "label", help="label filter candidates yourself (resumable, blind)"
    )
    _common(p)
    p.add_argument("--dataset", required=True)
    p.add_argument(
        "--profiles", default="", help="comma-separated subset of profile names"
    )
    p.add_argument(
        "--relabel",
        action="store_true",
        help="revisit candidates that already have a label",
    )
    p.add_argument(
        "--labeller",
        default=_default_labeller(),
        help="name recorded on each label (default: your login)",
    )
    p.set_defaults(func=cmd_label)

    p = sub.add_parser("grade", help="grade filter runs against your labels")
    _common(p)
    p.add_argument("--dataset", required=True)
    p.add_argument("--runs", required=True, help="comma-separated run labels")
    p.add_argument("--misses", type=int, default=40, help="misses to list per run")
    p.add_argument("--out", type=Path, default=None)
    p.set_defaults(func=cmd_grade)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
