# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues in `agent-lore/influx`. Use the `gh` CLI for issue operations.

## Conventions

- Create an issue: `gh issue create --repo agent-lore/influx --title "..." --body "..."`
- Read an issue: `gh issue view <number> --repo agent-lore/influx --comments`
- List issues: `gh issue list --repo agent-lore/influx --state open --json number,title,body,labels,comments`
- Comment on an issue: `gh issue comment <number> --repo agent-lore/influx --body "..."`
- Apply or remove labels: `gh issue edit <number> --repo agent-lore/influx --add-label "..."` / `--remove-label "..."`
- Close an issue: `gh issue close <number> --repo agent-lore/influx --comment "..."`

The repository can also be inferred from `git remote -v` when running inside this clone.

## When a skill says "publish to the issue tracker"

Create a GitHub issue in `agent-lore/influx`.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --repo agent-lore/influx --comments`.
