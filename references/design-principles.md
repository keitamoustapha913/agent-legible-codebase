# Deep-module design for an agent-legible codebase

Use these rules when identifying the best destination for logic; avoid treating folder conventions as architecture.

## 1. Design around a behavior owner

For each task, name the domain outcome (e.g., calculate checkout discount, enforce inventory hold). Look for one discoverable module that owns the invariants and workflow. A file named `helpers.ts` with scattered pieces of the same rule is a poor owner; a precise domain module often provides a better entry point.

A useful module can comprise several private files. External callers should need only its small, explicit interface; internal complexity should be concentrated rather than copied into clients. A "deep module" need not be a single long file or be large in absolute size.

## 2. Learn the actual interface, not just the signature

An interface includes preconditions, postconditions, errors, ordering constraints, resource needs, and potentially performance. A two-method interface requiring eight implicit rules may be cognitively larger than a five-method interface with obvious behavior. Reduce *what callers must know*, not blindly the export count.

Prefer an action like `priceCheckout(cart, customer)` over an external flow that computes eligibility, calls discounts in a specific order, mutates cart totals, and then reconciles rounding in five files. Keep the implementation internally modular if that improves local reasoning.

## 3. Decide whether a seam earns its upkeep

Introduce an alternate adapter for a genuine variation point: e.g., persistent vs in-memory storage in tests, two actual providers, or expensive external I/O. Avoid speculative `IPriceCalculatorFactoryProvider` layers with one implementation. Dependency injection helps when it is local and straightforward; it harms navigation when it turns a feature into a multi-file scavenger hunt.

The deletion test: imagine removing a wrapper. If its code is easy to inline without teaching callers any new rules, its interface may be pure overhead. If its removal forces callers to duplicate decisions or understand hidden invariants, it was protecting useful complexity.

## 4. Make testability emerge from the interface

Exercise observable behavior through the same interface used by callers; mock only necessary external effects. Keep error semantics and real cross-module integration tests. Pure functions are useful when they capture a stable concept, not when extracting them merely hides the ordering bug in the caller.

Separating decisions from I/O can make verification faster, but do not split every decision into its own exported file. Multiple internal private functions can support one deep public operation.

## 5. Reduce navigation work with visible ownership

- Paths and names should match problem statements and business concepts.
- Imports should reveal dependencies. Avoid import cycles, magic registries, dynamic names, and broad wildcard barrels when they hide ownership.
- Keep related domain rules close to the behavior they govern, and keep tests near their natural owner according to the project's conventions.
- Do not duplicate a business rule in the HTTP layer, UI layer, and persistence layer.
- Keep an obvious entry point for a feature; if flow crosses files, order and handoff should be easy to trace.
- Do not introduce a generic abstraction unless at least two real consumers gain meaningful leverage.

## 6. When *not* to merge

Keep modules separate when they have independently stable responsibilities, change at different rates, are reused in unrelated domains, need isolation for performance/security, or represent genuine infrastructure variation. A huge module that all agents must read is less local than a few focused cooperating modules. Do not move functionality to satisfy a target file size or remove valid abstraction just to reduce tool calls.

## 7. Design it twice when expensive to reverse

For a consequential seam, produce two approaches that differ in ownership or interface shape, not merely naming. Compare:

1. Which callers have to learn the smallest, most stable set of rules?
2. Which approach keeps the highest-churn behavior closest to its tests?
3. How many files and dependencies must an unfamiliar agent inspect for common tasks?
4. Can existing callers and adapters migrate safely?
5. Which approach withstands the most likely next change without exposing internal state?

Select the better *trade-off*, not the design with the highest theoretical abstraction count.

## 8. Source is the deliverable

Existing `GLOSSARY.md`, ADRs, and contributor guidance may help name and preserve decisions. Read them when relevant, but do not substitute more docs or a giant `AGENTS.md` for readable source. Only update project docs if a genuine domain decision changed or the user asked.

### A hypothetical illustration (not a measured benchmark)

**Before:** `routes/checkout.ts` calls `utils/eligibility.ts`, `helpers/coupon.ts`, `config/promo.ts`, `totals/money.ts` in a strict undocumented order; several callers repeat reconciliation logic.

**After:** `checkout/pricing.ts` offers `priceCheckout(request)` and internally handles eligibility, promotion rules, rounding, and totals. External integrations stay behind the few seams that actually vary. Tests check `priceCheckout` outcomes and integration with real consumers.

This is appropriate only if all of those rules truly form one cohesive responsibility. It is not a prescription to merge unrelated payment processing, shipping, or persistence into pricing.
