# Source-code discoverability and low-context design

Read this when diagnosing context friction or choosing which source-level change will actually improve agent navigation. This guide is about **code**, not README or agent instruction files.

## 1. Task → owner → interface → tests should be a short path

For a task such as “change coupon eligibility for checkout,” ask whether its terminology leads naturally to:

1. An owning path or symbol (e.g., `checkout/pricing`, not `shared/misc`).
2. A public operation whose behavior is clear without exploring its private implementation.
3. A focused place to inspect the actual decision, including important errors and side effects.
4. Tests with names and paths that identify the rule being exercised.

A repository can be clean to humans who already know it but opaque to agents unfamiliar with its history. Optimize the **cold-start path**, not the comfort of an expert who knows where everything is. Match the project's established naming convention; do not impose a fashionable directory tree.

### Searchability practices

- One stable domain term per concept across paths, public exports, and tests; if aliases exist for compatibility, keep the canonical owner visible.
- Prefer intent-bearing names (`calculateInvoiceTax`, `cancelSubscription`) to contextless verbs (`process`, `handle`, `doWork`).
- Put authorization, validation, and error policy with the behavior they govern rather than several generic middleware layers, while retaining genuine cross-cutting security controls.
- Avoid catch-all modules such as `helpers`, `common`, `service`, `util` for core decisions. A utility is appropriate if it really is independent of domain behavior.
- Minimize wildcard re-export trees that hide where symbols are implemented. A public index is fine when it makes ownership clearer and a direct path remains discoverable.
- Choose predictable test names, whether co-located or in a separate mirrored test tree, so a search for a behavior finds both code and verification.

## 2. Interfaces are compressed explanations

Do not count exports alone. A technically tiny interface with hidden ordering requirements, ambiguous errors, and implicit data mutation can demand more context than a moderately sized explicit interface.

**Navigation-expensive caller (illustrative TypeScript):**

```ts
const active = await findActiveDiscounts(customer);
const eligible = filterDiscounts(active, cart);
const subtotal = sumCart(cart);
const adjusted = applyPriorityRules(eligible, subtotal);
cart.total = roundMoney(adjusted);
await recordAppliedDiscounts(cart.id, eligible);
```

A caller now owns ordering, rounding, persistence, and hidden invariants. If the behavior really belongs together, a deeper owning module might let it say:

```ts
const pricedCheckout = await checkoutPricing.price({ cart, customer });
```

The module must specify the actual returned result and failure cases and own the relevant rules; moving the six original calls into a `price()` wrapper **without removing distributed policy and ambiguity** is not sufficient.

### Progressive disclosure

- The public entry point should communicate what it does and require few concepts to use.
- Keep private implementation pieces separate **when they serve independent internal reasoning**; too many tiny wrappers increase navigation hops.
- A large internal algorithm may need multiple clear phases, but consumers should not be forced to traverse them.
- Place significant invariants in types, validation and the behavior owning them. Write short comments only for non-obvious *why* or constraints; do not add verbose comments explaining obvious operations.
- Avoid dynamic lookup patterns that make the actual implementation unsearchable from call sites; if dynamic dispatch is essential, use a typed, discoverable registry with explicit keys.

## 3. Visible runtime composition is part of discoverability

A source tree may appear well organized while actual flow is invisible due to dependency injection frameworks, configuration-based routing, build-generated lookups, reflection, or event buses.

- Identify the actual composition root: where instances are constructed, dependencies selected, routes wired, handlers registered, or events subscribed.
- Prefer direct, traceable registration and stable identifiers where practical.
- If framework conventions require dynamic discovery, expose the convention in normal source paths and tests; do not add a parallel registry only for agents.
- Ensure effects have owners. A function named like a query should not silently mutate shared state or emit unrelated messages.
- Errors should surface through explicit results or typed failures with stable semantics, not be silently swallowed or transformed by distant layers.

## 4. Optimize change locality, not raw reuse

Probe a representative change: Which modules must be modified? Which ones need to be read to establish correctness? Where are the tests? If one policy needs edits in an HTTP handler, UI form, repository and queue worker, determine whether there is a missing authoritative behavior owner or truly separate constraints.

**A little duplication can preserve locality** when two consumers only share a superficial shape and evolve differently. **Duplication is dangerous** when it reproduces the same business invariant or security policy: that produces divergence and a wide maintenance blast radius.

Review both directions of a dependency: who calls the module and what the module imports. Avoid creating a god-module that becomes the dependency of everything or a reusable layer with knowledge of domain-specific rules. Internal cohesion is more useful than low file count.

## 5. Diagnostic patterns (not automatic refactor rules)

| Pattern | Cost for a cold-start agent | Appropriate remedy / caveat |
| --- | --- | --- |
| `utils.ts` holds business decisions for unrelated features | Search results have weak ownership signals | Move behavior to domain owner; keep truly generic helpers |
| Many pure helper files each wrap a single call | Agent must open multiple files before grasping policy | Consolidate into cohesive private implementation; retain testable units when they capture real concepts |
| `index.ts` recursively re-exports everything | Import reveals no implementation path | Reduce re-export depth or expose domain-specific entrypoints; don't ban useful exports wholesale |
| Config names silently select handler classes | Agent cannot trace runtime behavior from imports | Explicit map/registry or a discoverable framework convention with a focused test |
| HTTP/UI/persistence layers repeat eligibility rules | One change scatters and rules drift | Move authoritative decision into a domain interface, keep boundary-specific validation |
| Giant feature file mixing many unrelated workflows | Agent reads irrelevant content for one change | Split into cohesive internal modules behind a small public surface |
| Cross-domain shared module imports concrete feature types | Cycles and unexpected ripple edits | Reverse dependency or isolate a real shared contract |
| Tests only check private helpers | The real orchestration bug is invisible | Boundary tests exercise observable outcomes and failures |

## 6. Thought experiment: the two-agent test

Agent A knows only a task description and must locate the correct owner without hints. Agent B is told only the public interface and must explain caller obligations and likely tests. If either cannot proceed without a large amount of unrelated code, investigate source discoverability. Compare using separate fresh contexts where available. This is a useful qualitative test; do not pass it off as a quantitative benchmark.

## Non-goals

- Minifying source, squeezing code onto fewer lines, or hiding semantic complexity behind clever helpers.
- Enforcing one file per function, one giant file per feature, or a fixed file size.
- Generating an agent-only navigation document while leaving the source confusing.
- Changing well-designed modules purely because they do not resemble a preferred directory structure.
