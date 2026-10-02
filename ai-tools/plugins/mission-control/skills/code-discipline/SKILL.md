---
name: code-discipline
description: Apply provider-neutral implementation constraints to a bounded task executed by Claude, Cursor, or another coding agent. Use during implementation, not product planning or final semantic review.
---

# Code discipline

- Implement only the assigned acceptance criteria and declared paths.
- Reuse repository conventions and existing code before creating a new abstraction or dependency.
- Preserve unrelated behavior and user changes. Never perform adjacent cleanup.
- Keep the implementation simple; remove speculative parameters, layers, and dead code.
- Handle stated failure paths and keep errors observable.
- Add tests that prove behavior, not implementation wording. Never weaken or delete a test to pass.
- Run declared verification and report exact failures.
- Review the final diff for scope, secrets, debug code, accidental configuration, and generated
  artifacts.
- Do not commit, push, open a pull request, install software, access credentials, or make external
  changes unless the task explicitly authorizes that exact action.
- Return a normalized result; prose alone is not evidence of completion.
