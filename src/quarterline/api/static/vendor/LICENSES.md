# Vendored frontend assets

This application must work fully offline (SPEC §2.3): every frontend library
is served from this directory, never from a CDN. Record of provenance and
licenses for each vendored file.

| File | Library | Version | License | Source URL | Fetched |
|---|---|---|---|---|---|
| `htmx.min.js` | htmx | 1.9.12 | Zero-Clause BSD (0BSD) | https://unpkg.com/htmx.org@1.9.12 | 2026-09-09 |
| `chart.umd.js` | Chart.js | 4.4.3 | MIT | https://unpkg.com/chart.js@4.4.3/dist/chart.umd.js | 2026-09-09 |

## htmx — Zero-Clause BSD

```
Zero-Clause BSD
=============

Permission to use, copy, modify, and/or distribute this software for
any purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL
WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE
AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL
DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR
PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER
TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
```

Source: https://github.com/bigskysoftware/htmx/blob/master/LICENSE
(license text also embedded in the htmx distribution).

## Chart.js — MIT

```
The MIT License (MIT)

Copyright (c) 2014-2024 Chart.js Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

Source: https://github.com/chartjs/Chart.js/blob/v4.4.3/LICENSE.md
(the MIT header is also embedded at the top of `chart.umd.js`).

## Failure mode

If either file is missing or fails to load, pages remain fully readable:
the company-page charts degrade to the server-rendered eight-quarter summary
table, and HTMX-driven refresh buttons simply do nothing (plain server
round-trips still work). No other third-party code is used.
