/**
 * CLOP planning-sheet endpoint.
 *
 * This is the whole server side of `sheets.py`. It is a script *bound to the shared planning
 * spreadsheet*, deployed as a web app with "Execute as: Me" and "Who has access: Anyone". That
 * deployment is what lets the repo carry no Google credentials at all: the identity lives inside
 * the deployment, and the repo holds only its public /exec URL.
 *
 * Redeploying is documented step by step in README.md next to this file. Keep the two in sync.
 *
 * ---------------------------------------------------------------------------------------------
 * Wire protocol (POST, application/json)
 *
 *   request   {action: "read",  tab: "LePone(Z)", range: "R11"}
 *             {action: "write", tab: "LePone(Z)", range: "R11", values: [[42]]}
 *             {action: "batch", ops: [ {...}, {...} ]}   -- several of the above, one round trip
 *
 *   success   {ok: true,  values: [[...], ...]}          -- always 2-D, even for one cell
 *   batch     {ok: true,  results: [ {ok, values|error}, ... ]}   -- one entry per op, in order
 *   failure   {ok: false, error: "..."}                  -- definitive; the client will not retry
 *   transient {ok: false, error: "...", retry: true}     -- the client will retry this one
 *
 * `batch` exists for reliability, not speed. Every request to this endpoint is an independent
 * chance to hit Google's expiring-result-link fault (see doGet below), so a sync built from eleven
 * separate requests had eleven chances to fail; as two, it has two. At a 95% per-call success rate
 * that is the difference between a sync completing 57% of the time and 90% of the time.
 *
 * Ops run in order and are NOT atomic: op 3 failing leaves ops 1 and 2 applied, exactly as three
 * separate requests would have. Order matters to callers -- the stockpile snapshot writes its
 * timestamp last precisely so a failure can never leave the sheet claiming freshness it does not
 * have -- so this must keep running them in the order given.
 *
 * Everything answers HTTP 200. Apps Script cannot set arbitrary status codes reliably, so the
 * client decides success from the `ok` flag, never from the status.
 *
 * The exact string "no such tab: <name>" is load-bearing: GoogleSheet.tab_exists matches on it to
 * tell a missing tab apart from an outage. Do not reword it.
 */

/**
 * Why this function exists at all.
 *
 * /exec does not return the result of a POST. It runs the script and then 302s to a one-shot
 * https://script.googleusercontent.com/macros/echo?user_content_key=... link holding the output.
 * That link is consumed by the first read AND expires on a timer (measured on this deployment:
 * alive at 15 seconds, dead at 30). Read it twice, or read it late, and Google does not 404 --
 * it falls back to invoking this deployment over GET.
 *
 * With no doGet defined, that fall-through produced "Script function not found: doGet" as a 5KB
 * HTML page carrying HTTP 200, which looks exactly like success until the client tries to parse
 * it. Defining doGet turns that into an honest JSON answer the client already understands.
 *
 * It cannot do the actual work: the fall-through GET carries only Google's own user_content_key
 * and lib parameters, not the action/tab/range the caller asked for. Reporting clearly is the
 * most it can do -- and `retry: true` makes the client fetch a fresh link, which is the fix.
 */
function doGet(e) {
  return json({
    ok: false,
    retry: true,
    error:
      'this endpoint answers POST only. A GET reaching the script means the one-shot result ' +
      'link Google redirected to had already been read or had expired before it could be ' +
      'fetched. Retry with a fresh POST.'
  });
}

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return json({ok: false, error: 'empty request body'});
    }

    var request = JSON.parse(e.postData.contents);

    if (request.action === 'batch') {
      if (!request.ops || !request.ops.length) {
        return json({ok: false, error: 'batch needs a non-empty ops list'});
      }
      var results = [];
      for (var i = 0; i < request.ops.length; i++) {
        results.push(runOne(request.ops[i]));
      }
      // One flush for the whole batch rather than one per write: the pending commits only have to
      // land before this response is built.
      SpreadsheetApp.flush();
      return json({ok: true, results: results});
    }

    var single = runOne(request);
    SpreadsheetApp.flush();
    if (single.ok) {
      return json({ok: true, values: single.values});
    }
    return json(single);
  } catch (err) {
    return json({ok: false, error: String((err && err.message) ? err.message : err)});
  }
}

/** One read or write. Returns {ok:true, values} or {ok:false, error} -- never throws. */
function runOne(op) {
  try {
    var sheet = SpreadsheetApp.getActive().getSheetByName(op.tab);
    if (!sheet) {
      // Load-bearing wording -- see the header comment.
      return {ok: false, error: 'no such tab: ' + op.tab};
    }

    var range = sheet.getRange(op.range);

    if (op.action === 'read') {
      return {ok: true, values: range.getValues()};
    }

    if (op.action === 'write') {
      // setValues insists the block match the range exactly; a mismatch throws and is reported
      // rather than half-applied.
      range.setValues(op.values);
      return {ok: true, values: op.values};
    }

    return {ok: false, error: 'unknown action: ' + op.action};
  } catch (err) {
    return {ok: false, error: String((err && err.message) ? err.message : err)};
  }
}

function json(payload) {
  return ContentService
      .createTextOutput(JSON.stringify(payload))
      .setMimeType(ContentService.MimeType.JSON);
}
