// Tracks manual edits on the Movies sheet and appends them to History
// in the same format used by the Telegram bot:
//   - Rank changes        -> "Rank Changed"  (drives the web UI's trend arrows)
//   - Replaced/cleared    -> "Note Replaced" (keeps the previous review; read it
//     reviews                with /notehistory <title> in the bot)
//
// Setup:
//   1. Open the Google Sheet
//   2. Extensions → Apps Script
//   3. Paste this file's contents, save, and close
//   4. No manual trigger setup needed — onEdit fires automatically on human edits
//
// After updating this file in the repo, paste it into Apps Script again; the
// sheet only runs the copy that is saved there.

function onEdit(e) {
  const sheet = e.range.getSheet();
  if (sheet.getName() !== 'Movies') return;

  // Only handle single-cell edits (e.oldValue is unavailable for multi-cell)
  if (e.range.getNumRows() > 1 || e.range.getNumColumns() > 1) return;
  if (e.range.getRow() === 1) return; // skip header row

  const ss = e.source;
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const rankCol = headers.indexOf('Rank') + 1;
  const titleCol = headers.indexOf('Title') + 1;
  const notesCol = headers.indexOf('Notes') + 1;

  if (rankCol === 0 || titleCol === 0) return;

  const col = e.range.getColumn();
  const isRank = col === rankCol;
  const isNotes = notesCol !== 0 && col === notesCol;
  if (!isRank && !isNotes) return;

  const newValue = (e.value || '').trim();
  const oldValue = (e.oldValue || '').trim();
  if (newValue === oldValue) return;

  const title = sheet.getRange(e.range.getRow(), titleCol).getValue().trim();
  if (!title) return;

  const today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');

  let detail, type;
  if (isRank) {
    type = 'Rank Changed';
    detail = 'Movies: ' + (oldValue || '(blank)') + ' → ' + (newValue || '(blank)');
  } else {
    // Nothing to save if the cell was empty before the edit.
    if (!oldValue) return;
    type = 'Note Replaced';
    detail = 'Movies: ' + oldValue;
  }

  let histSheet = ss.getSheetByName('History');
  if (!histSheet) {
    histSheet = ss.insertSheet('History');
    histSheet.getRange(1, 1, 1, 4).setValues([['Date', 'Type', 'Title', 'Detail']]);
  }

  histSheet.appendRow([today, type, title, detail]);
}
