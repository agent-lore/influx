"""Model-eval harness for Influx's three LLM slots.

Measures a candidate model against a baseline on the exact requests
production sends — the filter scorer, Tier-1 enrichment and Tier-3
extraction — so a model swap (GPT-4.1-mini → GPT-6 Luna today, an
on-prem model tomorrow) is a measured decision rather than a hunch.

A human-labelled answer key for the filter (``evals label`` / ``evals grade``)
grades its decisions without relying on another model.

Run with ``uv run python -m evals --help``.  See
``docs/operations/model-evals.md`` (how to) and ``docs/plans/model-evals.md``
(background and results).
"""
