# Vendored third-party assets

These files are committed to the repository on purpose: the application must run with no
internet access at runtime, and the image policy forbids downloading prebuilt applications.
No CDN is contacted by the browser, and no Node toolchain is required to build them.

| File | Version | Source | SHA-256 |
|---|---|---|---|
| `htmx-2.0.10.min.js` | 2.0.10 | `https://cdnjs.cloudflare.com/ajax/libs/htmx/2.0.10/htmx.min.js` | `71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de` |

Verify with:

    sha256sum app/static/vendor/htmx-2.0.10.min.js

## Why htmx 2.0.10 and not 4.0.0

htmx 4.0.0 was the newest release at the time of writing, but it is a brand-new major line with
exactly one release and no patch versions yet. 2.0.10 is the mature line (eleven releases) that
every current example and answer targets. For a time-boxed build, an unfamiliar major with no
accumulated bug-fixes is a risk with no matching benefit.

`.gitattributes` marks this directory `-text` so Git never rewrites line endings here and the
recorded checksum stays true on every platform.
