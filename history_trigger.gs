// Tracks manual rank changes on the Movies sheet and appends them to History
// in the same format used by the Telegram bot.
//
// Setup:
//   1. Open the Google Sheet
//   2. Extensions → Apps Script
//   3. Paste this file's contents, save, and close
//   4. No manual trigger setup needed — onEdit fires automatically on human edits

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

  if (rankCol === 0 || titleCol === 0) return;
  if (e.range.getColumn() !== rankCol) return;

  const newValue = (e.value || '').trim();
  const oldValue = (e.oldValue || '').trim();
  if (newValue === oldValue) return;

  const title = sheet.getRange(e.range.getRow(), titleCol).getValue().trim();
  if (!title) return;

  const today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
  const detail = 'Movies: ' + (oldValue || '(blank)') + ' → ' + (newValue || '(blank)');

  let histSheet = ss.getSheetByName('History');
  if (!histSheet) {
    histSheet = ss.insertSheet('History');
    histSheet.getRange(1, 1, 1, 4).setValues([['Date', 'Type', 'Title', 'Detail']]);
  }

  histSheet.appendRow([today, 'Rank Changed', title, detail]);
}
