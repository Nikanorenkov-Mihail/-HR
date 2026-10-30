---
name: programming
description: Implement, debug, refactor, and review software in an existing or new project. Use for programming tasks across languages and frameworks; follow the repository's conventions and verify changes proportionally to the request.
---

# Programming

Use this skill for general software development work.

- Inspect the relevant project files and local instructions before changing code. Preserve the existing language, framework, architecture, and naming conventions unless the request calls for a change.
- For a new project or feature, clarify the expected behavior from the request and implement the smallest complete solution that fits the codebase. Avoid adding dependencies or broad refactors without a concrete need.
- For debugging, reproduce or trace the reported behavior, identify the cause, and fix that cause instead of masking symptoms.
- Keep changes focused. Handle errors and input validation at the boundary where untrusted data enters; avoid unnecessary abstractions and speculative defensive code.
- Verify the change using the project's existing checks when appropriate. Do not add or run tests unless the user asks for testing or verification. Report checks run and any limitations clearly.
- For external services, libraries, protocols, or APIs whose behavior may have changed, consult the current primary documentation when needed; do not rely on remembered versions or invented methods.
- Do not expose credentials, tokens, or private data. Load secrets from the project's established secret-management mechanism and never commit them.
