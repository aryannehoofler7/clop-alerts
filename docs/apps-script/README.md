# The Apps Script endpoint — source, and how to redeploy it

`Code.gs` in this folder is the entire server side of `sheets.py`. It is a script **bound to the
shared CLOP planning spreadsheet**, published as a web app. Its public `/exec` URL is committed in
`sheets.py` as `EXEC_URL`.

Until now this source was not actually in the repo — the design spec said it was, and it was not.
If the deployment had ever been lost there would have been nothing to rebuild it from. That is the
main reason this folder exists.

## Who needs to do this

Only the **owner of the spreadsheet** (whoever's Google account the deployment executes as). Nobody
else needs a Google account at all — that is the entire point of the design.

## When to redeploy

| Situation | Redeploy? |
|---|---|
| **You want `batch` live (see below)** | **Yes — this is the one that actually improves reliability** |
| You want the `doGet` fix live | It rides along in the same redeploy |
| The monitor says `'Anyone' access` repeatedly | Yes — the deployment has lost public access |
| The endpoint 404s permanently | Yes — the deployment is gone; you will also need to update `EXEC_URL` |
| A one-off garbled reply that clears itself | **No.** That is Google being slow. See below. |

## The `batch` change, and why it matters

This is the one worth redeploying for.

Every request to this endpoint is an independent chance to hit the expiring-result-link fault
described below. A sheet sync used to be **eleven separate requests** — three reads and eight
writes — so it had eleven chances to fail, and during a bad patch it reliably found one:

| Per-request success | Sync completes — 11 requests | — 2 requests |
|---|---|---|
| 95% | 57% | 90% |
| 90% | 31% | 81% |

The `batch` action takes a whole sync to **two** requests: one carrying all three reads, one
carrying every write. Ops run in order and are not atomic — op 3 failing leaves ops 1 and 2
applied, exactly as three separate requests would have.

**Nothing breaks if you never redeploy.** The client probes for `batch` once, and if the
deployment does not understand it, falls back to the current one-request-per-range path for the
rest of that run. It costs one wasted request per monitor start and improves by itself the moment
the new script goes live.

> Worth knowing if you ever touch this: the old script reads `request.tab` *before* it looks at
> `request.action`, so a `batch` payload makes it answer `no such tab: undefined`. The client
> classifies "no such tab" as a definitive configuration error, so a fallback that keyed off the
> error *wording* would have switched sheet sync off permanently against exactly the deployment it
> exists to support. Detection uses *where* the failure happened instead — an outer rejection means
> "did not understand", a failure inside `results` means "understood fine, one op was bad".

## The `doGet` change, and why it matters

`/exec` does not return the result of a POST. It runs the script and then redirects (302) to a
one-shot `script.googleusercontent.com/macros/echo?user_content_key=…` link holding the output.
Measured on this deployment, that link:

- is **consumed by the first read** — a second read of the same link fails; and
- **expires on a timer** — still good at 15 seconds, dead at 30.

When the link is read twice or read late, Google does not return a 404. It falls back to invoking
the deployment **over GET**. The original script defined only `doPost`, so Google answered
`Script function not found: doGet` — as a 5 KB HTML page carrying **HTTP 200**, which is
indistinguishable from success until the client tries to parse it as JSON.

That is the fault behind the `Sheet sync failed …` dialogs. It is Google-side and transient; the
sheet, the data and the deployment's access setting are all fine. Measured live: after 56 polls
answering in 3–6 seconds, one poll saw the script take 21.8 seconds and the result-link fetch a
further 16.3, so the link expired mid-fetch.

`Code.gs` now defines `doGet`, so that fall-through returns

```json
{"ok": false, "retry": true, "error": "this endpoint answers POST only. …"}
```

which `sheets.py` already understands and retries. **Redeploying is optional** — the client
recognises the HTML page by name and handles it either way. Redeploying just makes the failure
honest on the wire instead of requiring the client to recognise a Google error page by sight.

> Note the `retry: true` flag. Every other `{ok: false}` is a definitive verdict the client does
> **not** retry (`no such tab` will still be true in three seconds). Without the flag, adding
> `doGet` would have swapped a retried HTML page for an un-retried JSON error — worse, not better.

## Redeploying, step by step

1. Open the shared planning spreadsheet in a browser, signed in as the **owner**.
2. **Extensions → Apps Script.** The bound script project opens in a new tab.
3. Select everything in the editor's `Code.gs` and replace it with the contents of
   `docs/apps-script/Code.gs` from this repo. Save (**Ctrl+S**).
4. **Deploy → Manage deployments.**
5. On the existing deployment, click the **pencil (Edit)** icon — do *not* use "New deployment"
   unless you intend to change the URL.
6. Set **Version** to **New version**. Leave everything else as-is; confirm they read:
   - **Execute as:** *Me (your account)*
   - **Who has access:** *Anyone*
7. Click **Deploy**.
8. The `/exec` URL is unchanged. Nothing in the repo needs editing.

> **If you use "New deployment" instead**, Google issues a *different* `/exec` URL, and you must
> paste it into `EXEC_URL` in `sheets.py` and commit that. Editing the existing deployment is
> simpler and is what step 5 says.

### "Who has access: Anyone" vs "Anyone with Google account"

It must be **Anyone**. "Anyone with Google account" makes Google serve a sign-in page to this
tool, which has no account — that is the failure the client reports as
`is the deployment still 'Anyone' access?`.

## Verifying the redeploy

From the repo root:

```bash
python sheets.py          # read-only: resolves CLOP_NATION, confirms the tab, reads one cell
python stockpiles.py      # read-only: reports where every block resolved to
```

`python sheets.py` printing your nation tab and a cell value means the endpoint is answering
correctly. Neither command writes anything.

To confirm the `doGet` path specifically, fetch the `/exec` URL with a plain GET in a browser. Before
the redeploy it renders **`Script function not found: doGet`**; after it, the JSON above.
