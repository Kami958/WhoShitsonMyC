AI-only frontend vendors (lazy-loaded when modules.ai is available).

- marked.min.js  — Markdown → HTML (marked@15 UMD: lib/marked.umd.js)
- purify.min.js  — HTML sanitize (DOMPurify@3 dist/purify.min.js)

Not loaded by index.html. Loaded from web/js/ai.js via _aiEnsureMarkdown().
Excluded from lite builds: python build.py --no-ai
