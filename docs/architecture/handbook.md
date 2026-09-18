# Modular Vertical Architecture — Adaptation Handbook

This handbook explains ways to apply the compact [MVA constitution](../../ARCHITECTURE.md). It is advisory. Words such as “prefer”, “consider”, and “often” are deliberate: the right shape depends on the application, language, team, risk, and evidence in front of you.

## 1. Use the framework as a lens

MVA is most useful for answering six questions:

1. What responsibility is being changed, and who owns it?
2. What complete behaviour produces the requested outcome?
3. Which decisions carry consequential meaning?
4. What may consumers rely on, and what remains private?
5. Which state and effects are authoritative here?
6. Which boundaries can fail or change independently?

If the existing project already answers those questions clearly, preserve its language and structure. Renaming folders to match MVA can add churn without adding understanding.

### When an artefact earns its place

| Artefact | Add it when | Usually skip it when |
|---|---|---|
| `module.yaml` | ownership, dependencies, or public surface are hard to discover | there is one obvious responsibility and the code says enough |
| `feature.yaml` | a behaviour crosses files, people, effects, or rollout steps | the behaviour is tiny and its tests/types tell the story |
| Policy Contract | a decision is consequential, disputed, regulated, or easy to misread | logic is purely technical or trivial |
| ADR | a trade-off, exception, or irreversible choice will matter later | the choice is routine and cheap to reverse |
| migration wave | old and new paths coexist or data/write ownership moves | a direct replacement is safe and observable |
| automated architecture check | a recurring violation can be detected with low false confidence | the property needs human/domain judgement |

Delete optional template fields that do not serve the project. Empty ceremony hides the important parts.

## 2. Find boundaries from responsibilities

A useful module usually has a coherent reason to change, vocabulary, policy, state authority, and consumer surface. None of those alone proves a boundary.

Signals that responsibilities may differ:

- different people decide their policy;
- the same word has different meanings;
- state has different lifecycle or retention;
- one side may change without coordinating every consumer;
- security or compliance ownership differs;
- a stable contract exists between them;
- operational scaling or failure isolation has demonstrated value.

Signals that a proposed split may be artificial:

- both sides always change together;
- the boundary merely mirrors controller/service/repository layers;
- most calls expose internal entities;
- transactions continually need both sides;
- the split exists only because a diagram expects more boxes.

Start coarse. Splitting a coherent responsibility later is usually easier than repairing prematurely distributed ownership.

### Boundary probes

Ask:

- Who can approve a semantic change here?
- Which state is this responsibility allowed to write?
- What could a consumer rely on without knowing the implementation?
- Which changes should remain private?
- Does this boundary reduce the context needed for one correct change?

The answers may identify a module, a package, a component, a process, or simply a well-named file. MVA does not require a particular granularity.

## 3. Keep behaviours vertical

A slice is a locality principle: the shortest coherent path from trigger to observable outcome. It need not be one directory or one class.

Keep close where practical:

- input and output language;
- behaviour-specific validation and authorisation;
- orchestration and policy invocation;
- owned state changes and effects;
- outcome/failure translation;
- focused tests and telemetry names.

Move something outward when it has a genuinely broader owner or lifecycle: shared module policy, a public contract, a technical adapter, host wiring, or a reusable mechanism with stable semantics.

### Choose the lightest slice shape

**Direct slice**

Useful for simple reads, straightforward writes, file transformations, CRUD-like administration, and low-consequence behaviours. Direct use of the framework or data library can be clearer than an artificial port.

```text
request/event/input → validate → query/change → result
```

**Protected policy slice**

Useful when eligibility, pricing, risk, allocation, permissions, classification, or lifecycle decisions need independent review and examples.

```text
input → gather explicit facts → pure decision → apply owned effects → result
```

**Workflow slice**

Useful when steps span time, remote systems, human action, retry windows, compensation, or independently deployed owners.

```text
trigger → record state → attempt step → wait/retry/compensate/escalate → outcome
```

**Custom shape**

Use the native form of a compiler, game loop, data pipeline, plugin, notebook, embedded loop, UI state machine, or library API. Preserve the ideas that apply rather than wrapping it in application-shaped ceremony.

## 4. Separate policy from mechanisms

Use this test:

> Could changing this logic alter a permission, amount, classification, obligation, allocation, or meaningful state transition without changing transport or storage?

If yes, the logic is probably policy. Policy benefits from business vocabulary, explicit inputs, stable outcomes, boundary examples, and an identified decision owner.

Mechanisms include parsing protocols, mapping rows, serialising messages, retries, cache access, selecting HTTP status codes, and invoking vendor SDKs. Validation may be either:

- “field parses as an integer” is normally input mechanism;
- “quantity must be positive to place an order” is business policy;
- “this actor may approve above a threshold” is business authorisation policy.

### Pure does not mean elaborate

Policy can be a function over plain values. It does not need entities, interfaces, dependency injection, or a rules engine.

```text
decision = decide(input, explicit_facts)
```

The caller may obtain current time, rates, identity, classifications, availability, or provider results and pass them in. The decision should expose unknown inputs and uncertain outcomes rather than inventing a convenient default.

### Policy Contract workflow

For consequential policy:

1. state the decision and its owner;
2. define terms, inputs, outcomes, reason codes, and precedence;
3. add approved examples and boundary cases;
4. record unknowns and external fact ownership;
5. make examples executable where the value justifies it;
6. implement the smallest model that preserves the agreed meaning;
7. update contract, examples, tests, and code together when semantics change.

An `approved` label is a claim about human authority. A validator can check that an owner and examples are present; it cannot prove genuine approval.

## 5. Design public contracts deliberately

A module's public surface is what named consumers may rely on. It can be an in-process function, type, protocol, command, query, event, schema, file format, library API, or network endpoint.

Good public contracts tend to:

- use provider-owned vocabulary;
- expose stable inputs, outcomes, and failure meaning;
- avoid persistence records and framework request objects;
- hide internal orchestration and policy types;
- name their intended audience and compatibility expectation.

“Public” has different strengths:

| Audience | Typical compatibility posture |
|---|---|
| one slice in the same commit | coordinated change may be enough |
| several modules in one release unit | contract tests and explicit deprecation may help |
| separately released internal consumer | versioned schema and compatibility window often help |
| external customer or partner | published lifecycle, security, migration, and support policy are usually needed |

Do not append `.v1` to every internal command merely because versioning is useful at remote boundaries. Version where consumers cannot safely change atomically.

### Provider contract versus consumer port

The provider owns its public contract. The consumer owns a port describing a capability it needs.

Use the provider contract directly when the call is local, the types fit, failure meaning is simple, and no translation or isolation is valuable. Add a consumer port when it protects a real seam, such as:

- vendor or protocol translation;
- several provider implementations;
- materially different failure semantics;
- persistence isolation for policy-heavy behaviour;
- provider volatility;
- a valuable test seam that cannot be achieved more simply.

Avoid `IRepository<T>`, `IService`, or `Manager` as default architecture. Prefer capability names such as `LoadCustomerRisk`, `ReserveCapacity`, or `PublishInvoiceIssued` when a port is earned.

## 6. Own data without over-prescribing storage

Ownership means authority to define meaning and write state. It does not automatically mean a separate database.

Possible enforcement strengths include:

- naming and code review;
- one write API or repository owned by the module;
- schema/table permissions;
- separate credentials;
- separate stores;
- separate processes or accounts.

Choose the least costly strength that handles the actual risk. A shared database can be a sound choice. Shared write authority usually is not.

Cross-boundary reads can use:

- a provider query;
- a consumer projection fed by events;
- a replica or warehouse with read-only semantics;
- a carefully documented reporting query.

The route should not accidentally grant write ownership or make a consumer dependent on private storage details without acknowledging that coupling.

### Transactions and dual writes

Prefer one authoritative local commit. When effects cross a failure boundary, consider durable publication, idempotency, reconciliation, compensation, or a workflow. Which techniques are needed depends on cost of loss, duplication, delay, and ambiguity.

For a migration, make stages explicit:

```text
observe old path
→ introduce target alongside it
→ compare outcomes/state
→ transfer authoritative writes
→ reconcile
→ remove old writes and compatibility path
```

Not every migration needs every stage. Never describe both paths as authoritative at once without a conflict rule.

## 7. Treat failure as part of behaviour

For each material dependency, ask:

- Can it time out after succeeding?
- Is retry safe, and what identifies a duplicate?
- Can responses arrive out of order?
- What does an unknown result mean to the caller?
- Who reconciles or compensates?
- What state and telemetry support recovery?

In-process calls can still fail, but remote calls add partial failure and temporal uncertainty. Extracting a service changes architecture because it changes failure semantics, not because the folder moved.

Use events when delayed independent reactions are acceptable and the event describes a completed fact. A message named like a command but broadcast as an event hides responsibility. When publication must survive a local commit, an outbox or an equivalent durable hand-off may be warranted.

## 8. Enforce properties, not aesthetics

An architecture check should state exactly what it observes.

Good check descriptions:

- “No Python import in `orders` resolves under `catalog/private`.”
- “Only the billing credential can update these tables.”
- “Published event schemas remain backward compatible for the supported window.”

Misleading descriptions:

- “Modules are isolated” when only one import spelling is scanned.
- “Policy is pure” when tests merely run without a database.
- “Data is owned” when a YAML file names an owner but permissions are unrestricted.

Use language-native dependency tools where possible. They understand aliases, relative imports, generated code, build graphs, and conditional compilation better than a universal text scanner.

The bundled `mva` validator checks only manifest structure and declared relationships. An open catalogue reports references to not-yet-catalogued artefacts as advisories; a project can select closed-catalogue errors when its configured set is intended to be complete. Declared cycles are also advisory unless local dependency semantics justify promoting or disabling that check. A project should compose the validator with its own compiler, linter, database, contract, and runtime checks.

### Exceptions

An exception mechanism should map cleanly to the check it suppresses. If a check cannot apply an exception precisely, keep the exception as a manual decision instead of pretending it was enforced.

Useful exception fields are:

- exact rule or check ID;
- exact module, feature, dependency, or resource scope;
- reason and alternatives considered;
- owner and compensating control;
- expiry date or objective removal trigger;
- next review date when ongoing oversight matters.

Use proportionate governance. A small local deviation may need a code comment; a cross-module writer or security boundary usually deserves an ADR.

## 9. Testing by claim

Tests should target the risk being claimed.

| Claim | Useful evidence |
|---|---|
| policy produces agreed decisions | data-driven examples and boundary/property tests |
| slice orchestrates one outcome | focused slice test with meaningful fakes |
| adapter translates correctly | adapter test against provider protocol or real local infrastructure |
| public contract remains compatible | consumer/contract/schema compatibility tests |
| dependency boundary is protected | language/build graph test |
| workflow recovers | failure injection, retry, reconciliation, and restart tests |
| critical journey works | small end-to-end test across the real composition |

Test doubles should model the capability and its failures, not make every test pass by default. Unknown identities, records, or classifications should normally be explicit fixture data or explicit unknown outcomes—not silently successful fallbacks.

Policy examples are strongest when the same examples reviewed by decision owners are executed by tests. Avoid copying the examples by hand into a second test table that can drift.

## 10. Observability and operations

Technical metrics such as latency and error rate are necessary but may not reveal business failure. Consider recording:

- module/responsibility and slice/behaviour ID;
- outcome and stable reason code;
- dependency failure category;
- workflow state and retry count;
- reconciliation divergence;
- policy version or decision evidence when auditability matters.

Avoid high-cardinality or sensitive data in metric labels. Use structured logs or traces for detailed correlation and follow the project's privacy rules.

Operational artefacts are earned. A library may need no runbook. A critical independently operated service likely needs service-level objectives, alerts, recovery ownership, and tested procedures.

## 11. Deployment is a separate decision

Logical modules do not require microservices. Consider independent deployment only when it creates measured value through:

- independent scaling or availability;
- security or regulatory isolation;
- failure containment;
- incompatible technology/runtime needs;
- release ownership that cannot be solved more cheaply;
- materially different recovery objectives.

Before extraction, look for a coherent public contract, clear data ownership, no private table access, observable failure semantics, contract tests, retry/idempotency decisions, and an operating owner. Extraction adds network, compatibility, rollout, and recovery work; include that cost in the decision.

## 12. Structures that can work

These examples are alternatives, not a required tree.

**Capability folders in one application**

```text
src/
  orders/
    public/
    place_order/
    policy/
    adapters/
  catalog/
    public/
    ...
  host/
```

**Packages or build projects**

```text
packages/
  orders-contracts/
  orders-application/
  orders-adapters/
  catalog/
  app-host/
```

**Non-application data product**

```text
pipelines/
  customer_health/
    inputs/
    classify_accounts/
    policy/
    published_dataset/
```

**Small tool or library**

```text
src/
  parse_input.*
  classify.*
  write_output.*
tests/
```

The small tool can still have explicit ownership and policy without inventing modules.

## 13. Change review prompts

Use only relevant prompts.

**Ownership and locality**

- What owns this behaviour and its state?
- Can a reviewer find the whole change without loading unrelated architecture?
- Does new shared code truly have shared meaning and lifecycle?

**Policy**

- Is a consequential decision hidden in orchestration, mapping, or adapter code?
- Are all outcome-changing facts explicit?
- Are precedence and unknown-result behaviour agreed?

**Contracts and collaboration**

- Is the dependency on a deliberate public surface?
- Is a new port protecting a material seam or just adding indirection?
- Does compatibility strength match the real consumer audience?

**Data and failure**

- Is there one authoritative writer?
- Can any effect succeed without a known local result?
- Are retry, duplicate, reconciliation, and recovery needs explicit?

**Evidence**

- Which check could falsify the important claim?
- What cannot be verified automatically?
- Did the change update the artefact that owns its meaning?

## 14. Common failure modes

**Architecture as folder theatre** — names look right but ownership and dependency direction are unchanged. Fix the actual boundary or remove the ceremony.

**A port per dependency** — indirection expands while semantics remain coupled. Keep direct dependencies until a real seam appears.

**Shared becomes ownerless** — common code accumulates business policy from several modules. Return policy to an owner or name the genuinely shared capability.

**Events everywhere** — temporal coupling becomes invisible and failures are harder to reason about. Use direct calls for immediate answers and events for facts.

**The manifest is treated as proof** — declared ownership differs from source, credentials, or runtime behaviour. Pair declarations with evidence appropriate to the risk.

**Templates become mandatory forms** — teams fill unknown values with placeholders. Delete irrelevant fields and add local ones that carry real decisions.

**The reference example becomes the framework** — one language, folder tree, or domain is copied literally. Translate the concepts into the target project's natural form.

**Unknown external facts default to success** — missing classifications or identities silently pass. Model unknown explicitly and let approved policy decide its outcome.

## 15. Agent-readable architecture

Keep the root brief. An agent should normally need the constitution, one owner boundary, one behaviour, affected policy, and relevant contract or ADR—not the whole handbook.

Useful local files answer concrete questions:

- module: purpose, public surface, state and dependency ownership;
- feature: outcome, inputs, failures, effects, policy references, evidence;
- policy: exact business meaning and examples;
- ADR: why a non-obvious trade-off or exception exists.

Let tests, types, schemas, compiler checks, and database controls carry facts they can prove. Use prose for intent, trade-offs, uncertainty, and ownership. Report observed, inferred, suspected, verified, and unresolved facts distinctly.

## 16. Evolve from real pressure

Review architecture when evidence changes:

```text
private imports repeatedly leak       → add a package/compiler boundary
several writers conflict              → strengthen write ownership
module graph cycles                   → revisit ownership or coordinate with a workflow
policy disputes recur                 → add an approved, executable Policy Contract
provider failures affect outcomes     → expose failure semantics and recovery
release coordination dominates        → evaluate a build or deployment boundary
independent operations add value      → evaluate service extraction
check produces false confidence       → narrow its claim or replace it
```

Architecture is successful when it makes the next correct change easier to locate, reason about, and verify—not when every available artefact has been filled in.
