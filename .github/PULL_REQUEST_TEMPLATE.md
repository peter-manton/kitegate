## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## Checks

- [ ] `pytest` passes
- [ ] If this touches `src/kitegate/` or `adapters/openwebui/`, `dist/` is rebuilt (`python tools/build_owui_filter.py`, `python tools/build_owui_photo.py`)
- [ ] If this changes a pack, it is re-signed
- [ ] No real children's messages, names or secrets in tests, fixtures or logs
- [ ] A change that could let content through has a fixture in `tests/test_policy.py`
