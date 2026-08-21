# Bundled geocaching-cli

This is the Muteki-facing copy of https://github.com/zzzzzyc/geocaching-cli
at `06d9a752383e0c1217efad9265676b5523484f03`.

It includes the two commands Muteki's geocache mode needs:

- `gc serve` — loopback HTTP API (`/api/status`, `/api/show/<GC>`)
- `gc check` — GeoCheck / Certitude verifier

The Cloud Agent GitHub App cannot push to `zzzzzyc/geocaching-cli` (`cursor[bot]` 403),
so this tree is the copy that lands with Muteki.

Install into an **isolated** environment. Do not `uv add` this package into
Muteki's `.venv` — `pycaching` pins `urllib3` 1.x.

```bash
uv sync --directory third_party/geocaching-cli --extra dev
```

Worker coordinate math stays in `muteki/vendor/geocaching_cli` and does not
import Playwright or pycaching.
