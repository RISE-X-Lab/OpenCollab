# Issue: normalize every whitespace run in labels

`normalize_label` currently replaces literal spaces, but tabs, newlines,
non-breaking spaces, and repeated whitespace can leak into the returned label.

Acceptance criteria:

- Trim leading and trailing whitespace.
- Convert each internal run of Unicode whitespace to one hyphen.
- Lowercase all non-whitespace characters.
- Return an empty string for whitespace-only input.
- Keep the public function signature unchanged and do not modify tests.

Team workflow:

1. The analyst inspects `labeler.py` and `tests/test_labeler.py`.
2. The analyst delegates the implementation to exactly one coder.
3. After the coder finishes, the analyst delegates independent verification to
   exactly one tester.
4. The analyst reports the implemented change and the tester's exact test result.
