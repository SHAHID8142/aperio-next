# Known limitations

Stated plainly, with the reasons. Nothing here is hidden in code, docs,
or reports.

## Trust and identity

- **Hashes are integrity checks, not authentication.** Content, chain,
  and manifest hashes prove bytes did not change; anyone who can write a
  file can recompute them. Chain hashes cannot detect the removal of
  complete trailing records without an external anchor.
- **Actor identifiers are declarations.** Reviewer, proposer, and import
  identities are recorded attribution, never proof of who acted. A
  caller that fabricates a complete, self-consistent review transition
  is indistinguishable from a store that genuinely recorded one.
- **A clean report is not a certificate.** A quality report, preflight,
  or benchmark with no findings means "nothing explicitly required was
  found missing" — not that the work is correct.

## Detection and judgment

- Structural checks only. The quality engine cannot judge truth, taste,
  beauty, or completeness that is not objectively checkable; paraphrased
  or semantic-only flaws are out of scope by design.
- `Q-source-support-unknown` is the honest ceiling: when sources are
  cited, whether they support the claims is unknown to the tool and
  needs human review.
- The benchmark measures Aperio's tooling on versioned fixtures with
  planted, objectively checkable flaws. It does not measure whether AI
  agents perform better with Aperio, and it produces no aggregate
  quality score.

## Memory

- Retrieval is keyword/scope based, not semantic: paraphrase can be
  missed and a shared word can match.
- Sensitivity is a filter, not encryption: stores are plain text; do not
  store secrets.
- A damaged store refuses further appends until repaired by hand;
  existing complete records stay readable. No crash/power-loss atomicity
  is promised (writes are durable via fsync; the directory entry is not).

## Concurrency and durability

- Advisory locks coordinate aperio writers with each other; tools that
  ignore the locks bypass them. CAS guards have narrow documented
  residual windows against non-cooperating writers. Abandoned locks are
  resolved by explicit user action; they are never stolen.
- Import is not a filesystem transaction: a hard crash can leave a
  subset, which `integrity` diagnoses and a re-run finishes.

## Brain Dump, orchestration, and skills (v1.1)

- Classification is keyword triage: a message about the task can be
  routed "related" by a shared word, and a genuinely related item
  phrased without overlap lands as ambiguous. Every item keeps its raw
  text and the operator reviews before anything becomes work.
- Urgency authority is the operator's `--urgent` flag only; detected
  urgency language has no authority and is reported, never acted on.
- Orchestration is sequential-only: parallel delegation is not offered
  because write-scope isolation cannot be proven for the existing
  adapters (an assessment never spawns anything; dispatch stays behind
  the ordinary gates).
- Skills are guidance, not verified improvements: no performance claims
  are made for any pack; conflicts block composition; approval records
  a declared identity only.
- The loopback UI is read-only and token-gated, but same-machine
  processes can read the token from the process command line — use it
  on a machine you control.

## Verification coverage (this release candidate)

- **GitHub Actions has executed zero test steps for the entire
  0.13→1.1 series** (account billing lock). Every green claim in the docs and the
  release notes is from local runs: Python 3.14.7 and 3.12.14 (the
  latter against the built wheel), 1248 tests each.
- Python 3.11 and 3.13 are declared supported and exercised in the
  blocked CI matrix but were not installed on the verification machine.
- Windows is supported through the same code paths but no
  Windows-specific behavior is claimed as tested.
- Live provider end-to-end execution (real Codex CLI / Claude CLI
  services) is unverified: no credentials were used. Adapters are
  verified against fake executables and the documented interfaces, and
  fail closed when the real interfaces are absent.
- No PyPI publication, GitHub release, or external service was used in
  producing this release candidate.

## Deliberate non-goals

No databases, dashboards, cloud services, plugin loaders, executable
rule engines, model API access, credential handling, GUI applications,
or automatic learning. A GUI is not required; the CLI/library with
manual agent exchange is the completed core product.
