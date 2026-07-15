# Change Handoffs

## 2026-07-14 — Async validation DB access follow-up

### Files touched

- `services/validation.py` — converted validation database reads to awaited `adb` calls.
- `cogs/match.py` — invokes async validation directly inside the existing concurrent gather.
- `CHANGES.md` — created this append-only handoff log.

### Changed function signatures

```python
# Before
def check_stat_outliers(player_id: int, new_stats: dict) -> list[str]:

# After
async def check_stat_outliers(player_id: int, new_stats: dict) -> list[str]:
```

```python
# Before
def validate_submission(match_id: int, extraction: dict, player_votes: list[dict] | None = None) -> dict[str, Any]:

# After
async def validate_submission(match_id: int, extraction: dict, player_votes: list[dict] | None = None) -> dict[str, Any]:
```

Return payload from `validate_submission()` is unchanged:
`{"auto_accept": bool, "flags": {player_id: [flag strings]}}`.

### Assumptions

No assumptions were made; the follow-up explicitly required the async conversion and the only call-site update.

### TODOs / blockers for other files or developers

- `cogs/queue.py` sets `in_progress` after `+room`; the required upload gate accepts only `awaiting_result`.
- `services/vision_extraction.py` does not request `position` or `is_mvp`. The new flow routes these missing game-provided fields to review rather than guessing, as required.

### Searches performed

- Ran `rg -n "validate_submission\\(|check_stat_outliers\\(|from services import .*validation|import validation" . -g '*.py'` to locate all validation imports and callers.
- Ran `rg -n "validate_submission\\(|check_stat_outliers\\(|from database\\.db import db|db\\." . -g '*.py'` after editing to verify validation has no synchronous `db` access and to re-check callers.

## 2026-07-14 — Queue result-state and OCR-field follow-ups

### Files touched

- `cogs/queue.py` — changes the host `+room` transition to the single result-upload state, `awaiting_result`.
- `services/vision_extraction.py` — requests game-provided per-player position and MVP fields through the shared extraction prompt.
- `CHANGES.md` — appends this handoff entry.

### Changed function signatures

None.

### Assumptions

"I assumed the existing final legibility/no-guessing paragraph is the single shared instruction section, so I extended that paragraph rather than creating provider-specific wording."

### TODOs / blockers for other files or developers

None. The previously logged `queue.py` state-transition and shared OCR-schema blockers are resolved by this entry.

### Searches performed

- Ran `rg -n -C 2 'in_progress' . -g '!**/__pycache__/**'` across the repository before editing. It found the `queue.py` write, the prior `CHANGES.md` handoff note, and status-constraint entries in `database/schema.sql` and `database/migration_ro3_verification.sql`; no embeds, admin, digest, or stats logic depends on `in_progress` for this state.
- Inspected `awaiting_room`, `awaiting_result`, and `+room` handling in `cogs/queue.py`; the preceding transition writes `awaiting_room`, and the host room-code handler is the only subsequent state write.
- Inspected all provider classes and `_PROVIDERS` in `services/vision_extraction.py`; Anthropic and NVIDIA pass `EXTRACTION_PROMPT`, while OpenAI and Qwen are unimplemented stubs with no hardcoded schema copy.
