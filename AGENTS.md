# Repository instructions for AI agents

## Required context before changing code

1. Read `/ARCHITECTURE.md`.
2. Read the target module's `module.yaml` and nearest `AGENTS.md`.
3. Read the target slice's `feature.yaml`, code, and tests.
4. Read affected Policy Contracts.
5. Read relevant public contracts and ADRs.
6. Load extended-handbook sections only when needed.

Do not load or modify unrelated modules by default.

## Architecture rules

- Top-level code is organised by business-capability module.
- Application behaviour is organised by use-case slice.
- Consequential business policy is explicit, framework-independent, and testable.
- Modules may use only another module's public contract.
- Cross-module writes are forbidden.
- Public contracts must not expose persistence or internal policy entities.
- Ports are purpose-named and introduced only at meaningful boundaries.
- Direct in-process calls are the default inside one deployable.
- Events require fact publication or real temporal/failure decoupling.
- Module dependencies must remain acyclic.
- Service extraction requires documented operational evidence and an owner.

## Change classification

Classify each non-trivial change as one or more of:

- Mechanism
- Behaviour
- Policy
- Contract
- Ownership
- Topology

Policy changes require an approved Policy Contract or explicit human decision. Contract changes require consumer-impact analysis. Ownership and Topology changes require migration, rollout, rollback, and usually an ADR.

## Policy safety

Never infer consequential business policy from table names, enum values, comments, or failing tests alone.

When policy is unclear:

1. state the unresolved question;
2. separate confirmed facts from inference;
3. propose Policy Contract changes and examples;
4. do not bury a guess in implementation.

Do not change policy merely to make tests pass.

## Placement

- One behaviour: target module / target slice.
- Slice-local rule: target slice / policy.
- Rule shared by slices in one module: module / policy.
- HTTP, database, queue, file, or vendor translation: adapters.
- Cross-module collaboration: public contract only.
- One-slice boundary capability: port beside the slice.
- Shared module boundary capability: module-level port.
- Dependency wiring: module bootstrap or host.
- One-use helper: keep local.

Do not create speculative `Common`, `Shared`, `Core`, generic repositories, empty layers, brokers, modules, or services.

## Before implementation

State:

- business outcome;
- change classification;
- affected modules and slices;
- policy and contract impact;
- data ownership and migrations;
- security and observability effects;
- tests;
- rollout and rollback;
- unresolved assumptions.

## Verification

Run applicable checks in this order:

1. target policy tests;
2. target slice tests;
3. target module tests;
4. architecture tests;
5. contract tests when contracts changed;
6. adapter integration tests when adapters changed;
7. critical end-to-end journey tests.

Never claim a test passed without executing it. Never weaken an architecture test to permit the change.

## Completion report

Report:

- outcome delivered;
- files/components changed;
- policy rules changed or unchanged;
- contracts changed or unchanged;
- data migration and ownership impact;
- security and observability impact;
- tests and commands executed with results;
- unverified assumptions;
- rollout and rollback;
- remaining liabilities.
