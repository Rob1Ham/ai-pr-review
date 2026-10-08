# AI PR Review

Run bounded, evidence-led pull request reviews through independent OpenCode model lanes. This is a reusable starting point for reviewing repositories you control; configure a target and model routes before running it.

## How it works

1. Poll eligible pull requests or select one explicitly. Pin the current head and diff base.
2. Prepare immutable, content-addressed source snapshots. Changed secret-like, binary, symlink, unsupported-mode, or oversized evidence fails closed; larger reviews include tracked context subject to exclusions.
3. Run each configured model in an isolated OpenCode process with read/search access to its snapshot, without shell, edits, target-code execution, plugins, or task spawning.
4. Validate structured findings against the reviewed commit and changed-line locations, then publish separate attributed GitHub reviews. Durable receipts avoid repeating completed model/head reviews.

Model output is a candidate, not a verified vulnerability. Reviews are static; the tool does not execute PR code, tests, or PoCs. The sample policy is **advisory** (`blocking_severities: []`). A required check and an appropriate branch-protection policy are separate operator decisions. Polling can miss intermediate heads between polls; a durable webhook queue would be needed to guarantee every head.

## Install

Requires Python 3.11+, Git, GitHub CLI (`gh`), and OpenCode:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests
```

Copy `configs/pr-service.example.json` to a **local, untracked** `configs/pr-service.json`. Set actual `owner/repo`, branch names, enabled models and credentials for those providers. The example uses deliberately invalid model placeholders and non-existent repositories; it cannot access an actual repository as shipped. Restrict `GH_TOKEN` to only the chosen target repositories, with the GitHub permissions needed for reviews and checks. Model-provider credentials can be supplied through provider-specific environment variables or an explicitly selected OpenCode auth file.

Start with a one-off local review (no GitHub publication):

```sh
ai-pr-review review-local --config configs/pr-service.json \
  --repository owner/repo --number 123 \
  --run-dir runs/local/example --workspace-dir runs/workspace --mock
```

`--mock` validates wiring, snapshots and report shape without model inference. Remove it only when configured models and credentials are ready. `review-local` still reads the configured GitHub repository; it never publishes. For an explicitly authorized PR, `ai-pr-review review-pr` publishes model reviews and a check. `ai-pr-review serve-prs` polls continuously and can incur model costs.

For a frozen release-range campaign, see `campaigns/example-release.json`. Replace every synthetic repository, revision, lineage and count before using it; this example is a schema illustration, not a runnable real campaign.

## Boundaries

- Treat repository content, PR text and model output as untrusted data. Static snapshots do not make private source safe for arbitrary provider disclosure; get appropriate authorization for model-provider access.
- Keep provider tokens out of source, prompts and committed configs. Local state can retain private code excerpts and review results; protect and expire it accordingly.
- Review confidence, severity, and reproducibility separately. Human review remains responsible for final disposition.

Licensed under MIT. The sample configuration and tests use synthetic project identities and contain no target source or live review artifacts.
