# Security

This project protects children, so a way to get harmful content past it is a
security issue, not just a bug.

## Reporting

Report privately through GitHub: the repository's **Security** tab, then
**Report a vulnerability**. Please do not open a public issue or pull request
for:

- a message or reply that gets through the safety core or a pack when it
  should not (include the text, the pack and the adapter)
- a way to reach a model without screening, or to switch screening off
- a way to read children's messages, identities or the block log
- a way to load a pack that is not signed by a trusted key when signatures
  are required, or a bundled pack that is not the project's
- authentication or transport weaknesses in the gateway

False positives (an innocent question stopped) are welcome as public issues,
ideally with the question added to the fixtures in `tests/test_policy.py`.

## Scope

The code in this repository: the core, the packs, the gateway and the
adapters. Problems in Open WebUI, LibreChat, LiteLLM, NeMo Guardrails or a
model provider belong with those projects, though tell us if Kitegate should
defend against them.
