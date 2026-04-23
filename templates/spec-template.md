<!--
  Filename convention (see root CLAUDE.md → "Spec Filename and Version Convention"):
  the filename suffix (e.g. `-v0.1.md`) is chosen at file creation and STAYS STABLE.
  The `version:` field below is authoritative for the current version and bumps over
  time; the filename does NOT track it. Cross-references use the stable filename form.
  Only bump the filename for MAJOR semantic replacements (e.g. `-v1.md` at a full rewrite).
-->
---
openapi: "3.0"
info:
  title: ""              # Short, descriptive title — e.g. "Agent Metrics Contract" or "Service Check Types"
  version: "0.1"         # Semver (authoritative current version; bumps do NOT rename the file).
                         # Bump minor for backward-compatible changes, major for breaking changes.
  status: "draft"        # draft | review | approved | deprecated
  authors: []            # List of authors — e.g. [chris]
  updated: ""            # ISO 8601 date: YYYY-MM-DD
  scope: ""              # One sentence: what interactions or decisions does this spec govern?
  owner: "specs"         # Repo-root-relative path to the directory that owns this spec
  components:            # All repo components whose behaviour this spec defines or constrains
    - ""
---

# [Title]

> <!-- One sentence summary. Who reads this and why? -->

## 1. Scope

<!-- What exactly does this spec govern? State what is in scope and what is explicitly out of
     scope. Include a sentence on why this spec lives at this directory level. -->

## 2. Context

<!-- Why does this spec exist? Describe the problem, the forces at play, and any prior decisions
     or constraints that shaped the options considered. -->

## 3. Decision / Specification

<!-- The core content.
     - For decision records: state the chosen option in bold on the first line, then explain it.
     - For interface/protocol specs: define the contract precisely with numbered sub-sections.
     Use ### 3.1, ### 3.2, ... for sub-sections on complex specifications. -->

## 4. Schema / Interface Definition

<!-- Include this section when the spec defines a typed message format, API surface, or data
     schema. Use tables or code blocks.

     Example:
     ```json
     {
       "hostname": "string",
       "timestamp": "integer (unix seconds)",
       "cpu": { "percent": "float", ... }
     }
     ```

     Delete this section entirely (including the heading) if this is a pure decision record
     with no schema. Do not leave it blank. -->

## 5. Constraints

<!-- Non-negotiable rules implementors must follow. Numbered list. Each constraint should be
     specific enough to fail a code review if violated. -->

1. <!-- constraint -->

## 6. Rationale

<!-- Why was this approach chosen over the alternatives? List alternatives considered and the
     specific reason each was rejected. This section exists so future agents do not re-open
     settled decisions without new information. -->

**Alternatives considered:**

| Option | Rejected because |
| --- | --- |
| <!-- option --> | <!-- reason --> |

## 7. Open Questions

<!-- Unresolved questions that block this spec from reaching "approved" status. For each:
     state the question, note the impact if unresolved, and identify who should resolve it.
     When a question is resolved, remove it from this section, update the spec body, and bump
     the version. -->

- [ ] <!-- question — impact — owner -->

## 8. Changelog

| Version | Date | Summary |
| --- | --- | --- |
| 0.1 | <!-- YYYY-MM-DD --> | Initial draft |
