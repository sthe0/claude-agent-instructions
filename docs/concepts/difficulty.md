# Difficulty

> The foundational object of the whole system — everything else exists to remove one.

A **difficulty** is *a divergence between a desired state and the actual state.* The agent's universal job is to remove difficulties of any kind, and this repository is the plan for removing an arbitrary one.

Every rule, skill, hook, and memory file has a **functional ground**: the specific difficulty it removes. Stating that difficulty — the X in "to achieve X, do Y" — is what lets you apply a rule correctly and generalize it. If you cannot name the difficulty a piece of this repo removes, treat that as a signal it is noise.

Difficulty is also the unit of learning. The loop the system runs is *difficulty → overcame it → recorded how → reused it next time* — see the [memory model](memory-model.md). Reuse has two layers above the recorded experience: a recurring difficulty is induced into a **regularity** (knowledge: how things are, refuted by a contradicting observation), and a **principle** (a norm: what to do for a goal class) cites the regularities it rests on in `grounded_in` and fails by not reaching its goal ([ADR-0013](../adr/0013-regularity-vs-principle.md)). When work is stuck, a difficulty takes an operational form (an actual result diverging from the expected one, or being unable to check at all); handling it is the difficulty-and-replan process, under **Processes** in the [documentation index](../README.md).
