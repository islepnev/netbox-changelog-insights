// Changelog Insights: show and hide the folded records of a change set.
// A click on the row (or Enter or Space on it) toggles the detail row; htmx loads it once.
(function () {
  function toggle(row) {
    var detail = document.getElementById(row.getAttribute('aria-controls'));
    if (!detail) return;
    var open = row.getAttribute('aria-expanded') !== 'true';
    row.setAttribute('aria-expanded', open ? 'true' : 'false');
    detail.classList.toggle('d-none', !open);
  }
  document.addEventListener('click', function (event) {
    var row = event.target.closest('.insights-changeset-row[aria-controls]');
    if (!row || event.target.closest('a')) return;
    toggle(row);
  });
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    var row = event.target.closest && event.target.closest('.insights-changeset-row[aria-controls]');
    if (!row || event.target !== row) return;
    event.preventDefault();
    row.click();
  });
})();
