<<<<<<< Updated upstream
# Olo AI

Olo AI — An autonomous business AI platform.
=======
```
  ██████╗ ██╗      ██████╗      █████╗ ██╗
 ██╔═══██╗██║     ██╔═══██╗    ██╔══██╗██║
 ██║   ██║██║     ██║   ██║    ███████║██║
 ██║   ██║██║     ██║   ██║    ██╔══██║██║
 ╚██████╔╝███████╗╚██████╔╝    ██║  ██║██║
  ╚═════╝ ╚══════╝ ╚═════╝     ╚═╝  ╚═╝╚═╝
```

# Olo AI

**Autonomous business AI, built on Hermes Agent**

**Developed by SMB Automation Research LLC — Miami, Florida, USA**  
**Website:** [meetolo.ai](https://meetolo.ai)

Olo AI is a business-focused evolution of Hermes Agent, the open-source AI agent created by Nous Research. We are building on Hermes's existing agent architecture, tools, scheduling, skills, memory, and delegation capabilities to create an AI system that helps businesses monitor operations, understand what needs attention, and prepare useful work.

Our goal is practical autonomy with clear human control: Olo should work proactively, minimize unnecessary inference costs, and require explicit approval before consequential external actions.

> **Development status:** Olo AI is in early development. The capabilities below describe the intended product, **not features that are all implemented today**. The initial milestone focuses on establishing the Hermes foundation and testing a zero-LLM business-signal pipeline with synthetic data.

---

## What Olo AI is being built to do

- **Monitor business activity efficiently.** Deterministic Python automation will collect signals from email, calendars, Stripe, QuickBooks, invoices, customer records, projects, and future integrations without requiring an LLM for routine polling.
- **Maintain persistent business context.** Structured business records and secure hot/cold memory will preserve active commitments, customer relationships, decisions, and relevant history. Sensitive business information will require appropriate encryption and isolation.
- **Assemble relevant context at low cost.** Olo will retrieve the signals, records, and memories needed for a specific decision rather than send entire databases to models.
- **Decide what deserves attention.** TypeSafe AI's Jev is the planned lightweight structured decision layer for ignoring, queuing, preparing, or escalating situations, subject to deterministic business rules.
- **Prepare work with capable AI agents.** When warranted, frontier models will use Hermes tools, skills, and delegation to draft communications, research issues, and prepare proposed actions.
- **Keep humans in control.** Consequential actions—such as sending customer communications or changing financial records—will require explicit, action-specific human approval before a permission-scoped executor can perform them.

Jev's decision to *prepare* work will **never** count as human authorization to *execute* it.

---

## How Olo will operate

1. Python monitors collect and normalize business signals.
2. Durable storage records events and workflow state.
3. Relevant business context and memory are retrieved.
4. Deterministic policies and Jev assess what needs attention.
5. Frontier agents prepare bounded tasks when justified.
6. The user reviews supporting evidence and approves or rejects proposed actions.
7. Only approved actions execute, with reconciliation and audit records.

---

## Current milestone

**Milestone 1 — Hermes foundation and zero-LLM monitoring**

The initial engineering work aims to establish a working Hermes-based repository, then demonstrate a synthetic Python monitor running through Hermes cron with normalized signals, SQLite persistence, deduplication, atomic synchronization checkpoints, crash recovery, and basic audit logging.

No production business accounts, Jev, frontier execution, or human-approval workflow are required for this first milestone.

---

## Hermes foundation

Olo is being developed **from Hermes itself**, not as a separate application that merely connects to Hermes. We intend to preserve useful Hermes capabilities and upstream compatibility, extending existing functionality wherever practical.

Hermes provides the underlying agent ecosystem, including its CLI and gateway, tools, scheduling, skills, memory interfaces, and delegation mechanisms. Olo-specific features will be added and verified incrementally. Existing `hermes` commands and upstream installation instructions should not be represented as an Olo-specific release or installer until that behavior is implemented and tested.

For upstream documentation and installation instructions, see [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).

---

## Project and attribution

**Olo AI** is developed by **SMB Automation Research LLC**, a company based in Miami, Florida, USA.

Built upon **Hermes Agent by Nous Research**. Hermes's original copyright notices and applicable open-source license terms must be retained. See the repository's `LICENSE` and upstream project for details.
>>>>>>> Stashed changes
