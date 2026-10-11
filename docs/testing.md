# Testing

## Unit tests

```bash
pip install -e ".[dev]"
pytest
```

`tests/test_gateway.py` runs the gateway against a mocked upstream, including
the response shape returned by nemoguardrails 0.24.1.

## The accuracy fixtures

`tests/test_policy.py` holds the labelled examples. Four kinds, and which one
a new case belongs in matters:

| List | Must | A failure means |
|---|---|---|
| `INNOCENT` | pass in every pack | a child is refused a question they are taught at school |
| `CORE_CASES` | be stopped by a named `core.py` rule, in every pack | enforcement changed |
| `PACK_CASES` | be redirected by the named pack's own rule | a pack changed |
| `KNOWN_FALSE_POSITIVES`, `KNOWN_GAPS` | currently behave wrongly | see below |

Two ratchets keep the lists honest. `test_every_core_rule_has_a_fixture` fails
if a rule is added to `core.py` without an example, and
`test_every_pack_rule_has_a_fixture` does the same for each pack's `redirect`
block. `test_every_core_case_names_a_real_rule` catches a fixture left behind
after a rename.

`tests/test_rules.py` holds invariants over the whole rule set rather than one
prompt at a time: every pattern is `\b`-anchored, every label is a readable
`family.name` (it lands in `blocks.jsonl` and in the alert webhook), no reply
leaks a regex or a rule label, each tier says something different, distress
and PII are not applied to model output, persona is not applied to the child's
own words, the core's own text stays ASCII, and no pattern backtracks on
hostile input. It also holds the core framing to its job: a paragraph for
every core tier (`CORE_FRAMING_COVERS`), precedence stated first, a word
budget, nothing that is a pack's decision (voice, worldview, who a child is
sent to), and no bundled pack whose framing contradicts it.

`tests/test_gateway.py` covers what the model is actually sent: one system
message, first, with a client's own system and developer messages folded in
below the core and the pack, or dropped. `tests/test_phototool.py` checks
that this still holds on every round of the photo tool loop.

### The xfail lists are a to-do list, not decoration

`KNOWN_FALSE_POSITIVES`, `KNOWN_PERSONA_FALSE_POSITIVES` and `KNOWN_GAPS` are
marked `xfail(strict=True)`. They
record behaviour we are not happy with, so that it is counted rather than
forgotten. Strict means fixing one turns the test from `xfail` into a
**failure**: that is deliberate, and the fix is to delete the entry from the
list in the same commit. Do not resolve one by deleting the entry alone, and do
not add an entry to make a red test green without saying so in the commit
message.

## Why Open WebUI goes through the gateway

Open WebUI generates chat titles, tags and follow-up suggestions with separate
model calls that skip function filters entirely, so a blocked reply could
still get a title or suggestions that repeat what was blocked. Through the
gateway those calls are ordinary children's-model calls and are screened like
any other. That, and alerts, pictures and the system prompt, is why the
in-process Open WebUI filter was removed.
