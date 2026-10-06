# Real-runs demo

`build.py` and `template.html` build the static demo published at
https://marthofdoom.github.io/oversteer/ : the web page's Live, Run and Coaching views on real runs,
with the coach's tips from the current code.

Refresh it (needs your own Oversteer telemetry databases, ACR runs):

    python3 scripts/demo/build.py --refresh

`--refresh` copies the live databases read-only (sqlite backup API; the originals are never opened for
writing), works the copies over with the repository's code, and writes `index.html` and `data.js` to
`~/.cache/oversteer-demo/site/` (set `OVERSTEER_DEMO_WORK` to use another folder). Without `--refresh` it
rebuilds from the copies already there. Dates in tip text are stripped.

Publish: copy `index.html`, `data.js` (and an empty `.nojekyll`) to the root of the `gh-pages`
branch, commit and push.
