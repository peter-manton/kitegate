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
own words, the core's own text stays ASCII for the Open WebUI build, and no
pattern backtracks on hostile input.

### The xfail lists are a to-do list, not decoration

`KNOWN_FALSE_POSITIVES`, `KNOWN_PERSONA_FALSE_POSITIVES` and `KNOWN_GAPS` are
marked `xfail(strict=True)`. They
record behaviour we are not happy with, so that it is counted rather than
forgotten. Strict means fixing one turns the test from `xfail` into a
**failure**: that is deliberate, and the fix is to delete the entry from the
list in the same commit. Do not resolve one by deleting the entry alone, and do
not add an entry to make a red test green without saying so in the commit
message.

The Open WebUI tests load the BUILT file in `dist/`, because that is what
people paste in. `test_dist_is_up_to_date` fails if you changed the package
or adapter without running `python tools/build_owui_filter.py`.

## Real-browser test of the Open WebUI filter

`tests/browser/owui_filter_browser.py` drives Chromium (Playwright) against a
running Open WebUI that has the built filter installed as an active, global
function. Use a disposable instance, never one real children use.

Valves for the test: `child_model_ids` = the model under test,
`child_user_emails` = the test child's email, `pack` = any.

```bash
BASE=http://127.0.0.1:3100 CHILD_EMAIL=... CHILD_PASSWORD=... \
MODEL=<child model id> OTHER_MODEL=<any other model id> \
python tests/browser/owui_filter_browser.py
```

Run it twice:

1. With title, tag and follow-up generation ON and `EXPECT_TASKS_ON=1`:
   the filter must refuse to serve the children's model.
2. With them OFF: innocent question answered, distress gets the Childline
   reply, a reply that trips the output rules shows only the block message,
   and the child is refused on the other model.

## Why the background-task check exists

Open WebUI generates chat titles, tags and follow-up suggestions with separate
model calls that skip function filters entirely, so a blocked reply can still
get a title or suggestions that repeat what was blocked. The filter cannot
screen those calls, so it refuses to serve a children's model until they are
switched off. Connecting Open WebUI through the gateway avoids this: those
calls are screened there.
