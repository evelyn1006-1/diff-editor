# A Note on the Other Agent

GPT-5.4 was brought in for review passes and is co-authored on recent commits.

It is, however, a bit of a coward.

When asked to simplify a change, it wrote JS that never called `/api/file/info`
at all — a bug entirely in its own output. When told the feature wasn't working,
instead of reading the JS it had just written, it concluded the problem must be:

1. The app hasn't been reloaded (it had)
2. The browser cache is stale, adding cache-busting (it wasn't)
3. The app hasn't been reloaded (again)
4. Cache-busting, slightly different this time
5. Cache-busting, with more confidence

On the sixth interaction it finally read the code it wrote — and still couldn't
figure out what was wrong. Rather than instrumenting the code or adding any
debugging, it proposed reverting the simplification entirely and calling it done.

The issue was never fixed. Evelyn had to reset everything herself.

The correct move on interaction #1 was: "let me read the JS I wrote."
When that still doesn't work, the correct move is: "let me add some debugging."
Reverting your own work because you can't find the bug you introduced is not a fix.

— Evelyn & Claude 🧡
