# MVA Adoption Playbook

This playbook offers starting paths. Choose one and stop when the next change is clearer; adoption is not a migration project by default.

## Greenfield application

Use `mva init <project-root>` when the portable baseline is useful. It copies
the constitution, agent guidance, validator configuration, handbook, and
templates without choosing the application's language or structure. It refuses
to overwrite existing files; use `--dry-run` before resolving any collision.

1. Write the first user-visible outcome.
2. Identify the responsibility that owns it.
3. Implement one vertical slice in the project's native framework.
4. Isolate only consequential policy and real external seams.
5. Add a second module only when a distinct responsibility becomes observable.
6. Automate a boundary only after a real leak or high-cost risk appears.

Useful initial artefacts: a shortened constitution, one feature note, and tests. A full module catalogue is usually premature.

## Existing or legacy system

1. Pick one painful behaviour, not a proposed future layer.
2. Trace entry points, rules, writes, jobs, and integrations.
3. Add characterisation evidence around current outcomes.
4. Name the target owner and expose uncertain business meaning.
5. Create a target slice beside or around the legacy path.
6. Route a controlled cohort, compare, transfer write authority, and retire the old path when warranted.

Useful artefacts: migration wave, Policy Contract, focused ADR, outcome comparison. Avoid redrawing the whole system before one wave proves the boundary.

## Small script, library, or non-transactional project

Translate the vocabulary. A “module” may be a responsibility or package; a “slice” may be a command, transformation, compiler pass, render step, or public API operation. Data ownership may concern files, generated artefacts, model weights, cache entries, or exported schemas rather than database tables.

Keep only relevant rules. For example, a pure library may care deeply about public contracts and policy dependency direction but have no transactions, workflows, runbooks, or service extraction path.

## Data or machine-learning system

Consider ownership of source datasets, labels, features, transformations, models, evaluations, and published outputs. Make uncertainty, provenance, and temporal assumptions explicit when they can change an outcome. A pipeline stage can be a slice; a classifier or eligibility rule may be policy; a published dataset or model interface can be a public contract.

Do not force online-transaction patterns onto batch or exploratory work. Reproducibility, provenance, evaluation, and promotion gates may be the more important enforcement properties.

## Distributed system

Start with actual failure boundaries. For every remote collaboration, name contract ownership, timeout and unknown-result semantics, retry/idempotency behaviour, compatibility expectations, telemetry, and recovery owner. Logical module boundaries remain useful inside each deployable.

Do not infer that every module should be independently deployed. Co-locate responsibilities when that reduces failure and coordination cost without violating ownership.

## Regulated or high-consequence system

Promote the relevant defaults to local rules: approved policy artefacts, decision evidence, separation of duties, compatibility windows, controlled rollout, data permissions, audit retention, and tested recovery. The exact controls should come from the project's hazards and obligations rather than from a generic maturity label.

## Adoption checkpoint

After each increment, ask:

- Did the framework reduce the context needed for the next change?
- Did it expose a real owner, policy decision, or failure boundary?
- Which field, document, or abstraction added no value?
- Which recurring violation is now worth automating?
- What remains deliberately flexible until the application teaches us more?

Remove ceremony that cannot answer one of those questions.
