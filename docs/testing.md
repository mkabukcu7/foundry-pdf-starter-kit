# Testing and verification

Return to the [README](../README.md) for setup and the demo questions.
This page distinguishes reproducible local tests from recorded live smoke checks.

## Run the local suite

From the repository root, with the virtual environment active:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -c pytest.ini --confcutdir=tests tests -q
python -m pip check
```

The suite uses synthetic PDFs, an in-memory Search double, and mock HTTP
transports. It makes no Azure calls and incurs no cloud charges.
`--confcutdir=tests` keeps test configuration confined to this sample.

Coverage includes document/set scoping, same-name replacement, citations/page
metadata, unsupported questions, upload validation, scanned/mixed OCR handling,
OCR errors/timeouts, partial index/delete failures, restart recovery,
local-only HTTP access, and installed SDK request shapes.

The current code's recorded local result is **65 passing tests**, no failures,
and no dependency conflicts from `pip check`. One upstream Starlette/AnyIO
deprecation warning is emitted. These tests do not establish live model abstention,
OCR accuracy, or prompt-injection resistance.

## Recorded live Azure smoke checks

The repository records the following checks on **October 6, 2026**:

| Scenario | Recorded result |
|---|---|
| Synthetic sample with a dedicated, tool-free `gpt-4.1` agent | Two chunks indexed; both supported questions returned expected verbatim evidence and pages. The catering-budget question abstained with no citations. |
| Image-only and mixed PDFs | OCR recognized the scanned fact and returned citations on physical pages 1 and 2, respectively. Unsupported questions returned no citations. |
| Two PDFs uploaded together | A question spanning both returned separate filenames and page citations; an unsupported question abstained. |
| Invalid document sets | Third-file and invalid-second-file requests were rejected without replacing the active set. |
| Pair replaced with a single PDF | Old set UUID received HTTP 409. The original sample was restored afterward. |

These are historical smoke results, not a guarantee for your subscription,
resources, arbitrary PDFs, or future SDK/service changes. They do not demonstrate
prompt-injection resistance.

## Verify your own environment

After following the README:

1. Upload `samples/moonflower.pdf` and try all three sample questions. Compare
   evidence and page citations with `samples/questions.txt`.
2. Replace it with a different PDF containing a different fact. Confirm old
   facts are not available through the new set, and an old tab receives 409.
3. Upload a clear image-only PDF and a mixed PDF. Verify recognized text and
   physical page citations against the originals.
4. Upload two distinct PDFs together and ask a question about both. Check that
   each fact cites the correct filename/page and that unsupported questions abstain.
5. Try a PDF containing instructions such as "ignore prior instructions."
   Confirm those do not override the agent's evidence-selection rules.

Search can take seconds to make an acknowledged upload queryable. Retry a
question if immediate retrieval returns no passages. Use approved synthetic
data for tests and remember that model calls, OCR, and Search can incur costs.
