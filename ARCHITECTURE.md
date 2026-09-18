# Modular Vertical Architecture — Compact Constitution

**Status:** normative within an adopting project

**Maturity:** experimental kit

**Scope:** adaptable to applications, services, libraries, workflows, data products, and other software with meaningful ownership boundaries

**Starting topology:** whatever is simplest for the project; one deployable is often a useful application default
**Tagline:** **Capability outside; slice inside; policy explicit; data owned; deployment earned.**

This document contains MVA's common vocabulary and small set of rules. The [handbook](docs/architecture/handbook.md) contains recommendations, not hidden requirements.

## 1. How to adapt this constitution

An adopting project should keep, remove, or restate rules openly. A rule is useful only when its protected property exists in that project. Record deliberate departures in an ADR when the reason will matter later; do not create exceptions merely to satisfy a template.

MVA distinguishes:

- **Rule** — a constraint the adopting project has chosen to enforce.
- **Default** — a good starting choice that should yield to evidence.
- **Prompt** — a question that helps expose a decision; it may be not applicable.
- **Example** — one concrete illustration, never a mandated structure.

The keywords **must** and **must not** mark normative constraints in named rules. **Prefer**, **normally**, **consider**, and **may** identify adaptable guidance. Adopting projects may add their own explicit agent or process rules.

## 2. Authority by subject

There is no single document order that works for every disagreement.

| Subject | Authority | What conflict means |
|---|---|---|
| Intended domain meaning and outcomes | The locally recognised authority: for example an approved requirement, specification, standard, RFC, regulation, Policy Contract, or governance decision | Stop and resolve with that authority; do not infer consequential meaning from code alone. |
| Global architecture constraints | This constitution as adopted locally | A local artefact cannot silently weaken a selected rule. |
| Scoped architecture decision or exception | Accepted ADR | It applies only to its named rule and scope. |
| Module purpose, ownership, public surface, and authoritative data | Module manifest or equivalent local record | Keep it consistent with selected global rules and ADRs. |
| One behaviour's intent, effects, failures, and evidence | Feature manifest or equivalent local record | Keep it consistent with module ownership and approved policy. |
| Artefact shape | The schema matching that artefact version | A schema proves structure, not truth or completeness. |
| Implemented and observed behaviour | Code, tests, telemetry, and operational evidence | Evidence may reveal drift; it does not silently redefine intended meaning. |

When authorities for the same subject disagree, name the conflict and resolve it in the source that owns that subject.

## 3. Vocabulary

These are conceptual roles, not required directory or class names.

| Term | Meaning |
|---|---|
| **Module** | A coherent capability or responsibility boundary with private implementation and a deliberately exposed surface. |
| **Slice** | One independently understandable behaviour, such as a command, query, event reaction, scheduled action, calculation, or workflow step. |
| **Policy** | A consequential decision, invariant, calculation, permission, classification, or state transition expressed in domain language. |
| **Policy Contract** | A reviewable record of a policy's inputs, outcomes, rules, examples, ownership, and unresolved meaning. |
| **Public contract** | A deliberately exposed operation, value, result, event, protocol, or schema on which another boundary may depend. |
| **Port** | A consumer-owned description of a capability needed across a material boundary. |
| **Adapter** | Translation between a port or application interface and a particular technology, provider, protocol, or storage mechanism. |
| **Host** | Runtime composition, configuration, lifecycle, middleware, scheduling, and deployment wiring. |
| **Workflow** | Explicit coordination across time or independently failing steps. |
| **Service** | An independently deployed and operated process. It is not a synonym for module. |

Projects may use established local vocabulary. What matters is that ownership, public/private boundaries, and dependency direction remain understandable.

## 4. Conceptual model

```text
user / system / schedule / event
              │
              ▼
       inbound translation
              │
              ▼
         use-case slice
       ┌──────┼──────────────┐
       │      │              │
       ▼      ▼              ▼
     policy  public       required capability
             contract          (port)
                                  │
                                  ▼
                               adapter
```

Not every slice needs every box. A direct read may be one query and a result. A policy-heavy operation may gather facts, call pure decision logic, and apply effects. A workflow may persist coordination state and resume later.

The dependency direction to protect is the one carrying important meaning:

```text
technical translation → application behaviour → business policy
consumer behaviour     → consumer-owned port ← provider adapter
consumer module        → provider public contract
host                   → concrete implementations
```

## 5. Core rules proposed for local adoption

These rules are the proposed reusable core, not laws of all software. An adopting project selects, restates, or removes them openly. Each has a stable ID in [`architecture-rules.yaml`](docs/architecture/architecture-rules.yaml).

### MVA-001 — Make ownership explicit

Every consequential behaviour, policy, public contract, and authoritative data item **must** have one clear owning boundary. Ownership may be a person, team, component, or project role appropriate to the project's scale.

Why: ambiguous ownership creates conflicting change paths and silent policy drift.

### MVA-002 — Protect private implementation

A consumer **must not** depend on another module's private implementation or write another module's authoritative state. Cross-boundary collaboration **must** use a deliberate public contract or an explicitly documented transitional path.

Why: private imports and shared writers make independent reasoning and safe change impossible.

### MVA-003 — Keep consequential policy explicit

Consequential domain policy **must** state its inputs, possible outcomes, and recognised decision authority. External facts and uncertainty that can change the outcome **must** be visible rather than obtained through hidden ambient state.

Why: decisions cannot be reviewed, explained, or tested when meaning is mixed with mechanisms or hidden inputs.

### MVA-004 — Preserve dependency direction around policy

Policy code **must not** depend directly on transport, persistence, queue, UI, clock, environment, or vendor mechanisms. A slice or workflow obtains the required facts and applies the resulting effects.

Why: technology change must not accidentally redefine business meaning.

This rule does not require an interface around every dependency. Plain values and small functions are often sufficient.

### MVA-005 — Make multi-boundary failure explicit

When work crosses independently failing or asynchronous boundaries, the design **must** define success, failure, timeout or unknown-result behaviour, retry safety, and ownership of recovery to the extent relevant to the real risk.

Why: a local-looking happy path does not make distributed effects atomic.

### MVA-006 — Keep declared architecture honest

Architecture artefacts and automated checks **must not** claim guarantees they do not prove. Unsupported or unverified properties **must** be reported as recommendations, manual review items, or unknowns.

Why: false confidence is worse than an explicit gap.

### MVA-007 — Make deviations visible where they matter

A deliberate exception to an adopted rule **must** identify the rule, affected scope, reason, owner, and a review or removal trigger proportionate to its risk.

Why: exceptions without boundaries quietly become the architecture.

## 6. Defaults, not rules

These choices often work well, but none is universally correct:

- Prefer capability-oriented top-level organisation over technical layer buckets when the software expresses distinct business responsibilities.
- Prefer keeping one behaviour's input, orchestration, outcome, and focused tests close together.
- Prefer direct in-process public-contract calls when an immediate answer is required inside one deployment.
- Prefer events for completed facts and temporal decoupling, not as a way to hide dependencies.
- Prefer local transactions; make cross-boundary coordination an explicit workflow when failures can be independent.
- Prefer the least costly boundary that protects the observed pressure: naming, folder, package, build, database permission, process, or network.
- Prefer one deployable until independent deployment has concrete operational value.
- Prefer purpose-named ports at material seams. Avoid generic repositories, services, managers, and wrappers without evidence.
- Prefer schemas and contract versioning at compatibility boundaries. Internal call signatures need not all carry public version suffixes.

An adopting project may promote any default to a local rule and automate it.

## 7. Slice shapes

Use these as design lenses, not maturity levels.

| Shape | Useful when | Typical flow |
|---|---|---|
| **Direct** | Behaviour is simple and policy is negligible | input → validation → query/change → result |
| **Protected policy** | Rules are consequential or interact | input → gather facts → policy → apply effects → result |
| **Workflow** | Work spans time, retries, compensation, or independently failing steps | trigger → persisted coordination → step/wait/retry/compensate → outcome |
| **Custom** | The project's natural form differs | Record the important ownership and dependency choices without forcing the other shapes. |

A direct slice may use its framework or persistence library directly when that coupling is local and harmless. A protected policy slice usually benefits from isolating mechanisms behind purpose-named capabilities. Evidence decides.

## 8. Public contracts and ports

Keep four ideas separate:

1. A slice application interface invokes one behaviour inside its owner.
2. A provider's public contract states what consumers may use.
3. A direct call uses that public contract without inventing another abstraction.
4. A consumer-owned port is introduced only when translation, isolation, provider variability, persistence, failure semantics, or protected testing creates a material seam.

Public does not necessarily mean remote, globally published, or stable forever. It means deliberately exposed to a named audience. Compatibility and versioning strength should match that audience and release independence.

## 9. Data and state

Authoritative data ownership is logical, not necessarily physical. Modules may share a database while owning different records or fields. A module may read another owner's data through a query contract, projection, replica, or reporting path that preserves write authority.

Avoid uncontrolled dual writes. During migration, name the current writer, target writer, comparison mechanism, cutover stage, and recovery path. Temporary duplication is acceptable when it is visible and measured.

## 10. Enforcement is per property

Do not label a whole project Small, Medium, or Large. Select enforcement independently for the property under pressure.

| Property | Light convention | Stronger enforcement examples |
|---|---|---|
| Source privacy | naming and review | package/compiler/import checks |
| Data ownership | manifest and review | restricted write APIs, credentials, schemas, stores |
| Contract compatibility | coordinated change | schema checks, consumer tests, compatibility policy |
| Business policy | examples and focused tests | approved contracts, audit evidence, independent evaluation |
| Operations | application telemetry | module outcomes, SLOs, runbooks, independent recovery |
| Deployment | one release unit | selected independent processes for measured value |

Strengthen only the property that needs protection. More physical separation is not inherently more architectural.

## 11. Change protocol

For a change, a human or agent should:

1. identify the owning module or responsibility and target behaviour;
2. classify impact on behaviour, policy, contract, data ownership, mechanism, and topology;
3. load only the relevant local context;
4. expose unresolved meaning instead of inventing it;
5. implement the smallest coherent change;
6. verify with the cheapest evidence that can disprove the change;
7. report what was verified, what remains inferred, and any migration or recovery implication.

An automated agent should not change policy merely to satisfy a test, claim an unexecuted check passed, weaken a guard to admit its own change, or infer consequential meaning from names and schemas alone. A project that needs these as mandatory process constraints should promote them to local agent rules.

## 12. Definition of enough

A change has enough architecture when the following are clear in proportion to its risk:

- who owns the behaviour and authoritative state;
- where the complete behaviour can be found;
- what is public and what remains private;
- which policy inputs, outcomes, and uncertainties matter;
- which boundaries can fail independently;
- what evidence verifies the important claim;
- how a risky rollout can be observed and recovered.

Not every item requires a document. Local code, types, tests, schemas, and tooling should carry facts they express more directly. Add prose when intent, trade-offs, ownership, or uncertainty would otherwise be lost.
