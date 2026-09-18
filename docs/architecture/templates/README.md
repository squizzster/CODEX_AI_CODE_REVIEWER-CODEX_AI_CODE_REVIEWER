# Architecture templates

These files are prompts, not mandatory forms. Copy the smallest relevant template, remove fields that do not help, and add project-native fields where needed.

| Template | Use when |
|---|---|
| [`module.yaml`](module.yaml) | a capability/responsibility needs discoverable ownership, contracts, data, or dependencies |
| [`feature.yaml`](feature.yaml) | one behaviour needs an explicit outcome, effects, failures, policy, or verification record |
| [`POLICY-CONTRACT.yaml`](POLICY-CONTRACT.yaml) | a consequential decision needs reviewable and executable meaning |
| [`ADR-TEMPLATE.md`](ADR-TEMPLATE.md) | a durable trade-off or architecture exception needs its reason preserved |
| [`MIGRATION-WAVE.yaml`](MIGRATION-WAVE.yaml) | an old and new route or writer coexist during a controlled transition |

Place instantiated YAML anywhere and pass explicit globs in `mva.config.yaml`, or use the default paths under `docs/architecture/{modules,features,policies,migrations}/`.

Angle-bracket placeholders make a template intentionally invalid. The validator ignores this template directory.
