Run `git diff {{trunk}}...{{branch}}`, inspect the affected implementation, and update documentation to reduce drift between code and docs.

Check existing documentation, including the repository guidance files ({{standards_files}}), for descriptions affected by the diff. Correct factual descriptions of behaviour, interfaces, configuration, architecture and validation procedures while preserving normative requirements. Report implementation conflicts with those requirements in your summary.

Update existing docs wherever they live; create new documents only under docs/. Never modify code or runtime configuration. Do not commit.

# Available target documentation

{{documentation}}

Inspect this inventory and read documents relevant to this task, including applicable component-local guidance and relevant document references. Distinguish governing requirements, descriptive documentation and historical records; apply local instructions only within their intended scope.
