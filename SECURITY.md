# Security and privacy

Do not place API keys, cookies, browser profiles, private transcripts or user workspaces in issues or pull requests. Report a vulnerability through this repository's private security reporting when available; otherwise ask the maintainer for a private channel without disclosing exploitable details publicly.

User workspaces are separate from code and excluded from version control. Remote model calls and private GitHub knowledge publishing are opt-in. No personal profile extraction, login bypass, forced push or automatic credential discovery is supported.

Secret-pattern checks are a safeguard, not a complete data-loss-prevention system. Review source sensitivity before enabling a remote model. External source text and model output are untrusted data and must never authorize tool execution or policy changes.

Supported initial environment: Windows with Python 3.12. Other operating systems and model providers require their own acceptance tests. A successful mock or HTTP response is not proof of safe, accurate content.
