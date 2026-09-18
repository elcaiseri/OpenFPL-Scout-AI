/* Recommendations use recorded evidence only; opening this view runs no jobs. */
(() => {
  'use strict';
  function suggestions(report, now = Date.now()) {
    const system = report.system || {}, archive = system.archive || {}, items = [];
    const add = (id, priority, title, detail, target) => items.push({id, priority, title, detail, target});
    if (report.official_status === 'unavailable') {
      add('official', 'Review', 'Restore the official data connection', 'Saved results remain available. Check service connectivity and refresh before relying on current results.', 'pipeline');
    }
    if (archive.enabled === false) {
      add('archive', 'Review', 'Enable forecast archiving', 'Enable the data archive in the service configuration before capturing another forecast.', 'pipeline');
    } else if (archive.last_result?.status === 'failed') {
      add('archive', 'Review', 'Resolve the last archive write error', 'Check the archive path, write permissions and service logs, then retry the capture.', 'capture-form');
    }
    const affected = [...new Set((system.models || []).filter(m => !m.loaded || m.last_inference === 'failed').map(m => m.name))];
    if (affected.length) {
      add('models', 'Review', 'Check unavailable model components', `${affected.join(', ')}: missing artifacts or a failed saved inference. Review the model diagnostics before the next capture.`, 'model-status');
    }
    const missing = [...new Set(system.feature_coverage?.entirely_missing || [])];
    if (missing.length) {
      add('features', 'Improve', 'Fill gaps in model inputs', `${missing.length} feature${missing.length === 1 ? '' : 's'} entirely missing in the latest saved inference. Inspect coverage and the source data before retraining.`, 'feature-coverage');
    }
    const upcoming = (report.gameweeks || []).filter(w => Date.parse(w.deadline_time) > now).sort((a, b) => Date.parse(a.deadline_time) - Date.parse(b.deadline_time));
    const next = upcoming[0];
    if (next && !next.eligible && archive.enabled === true && archive.last_result?.status !== 'failed') {
      add('forecast', 'Next step', `Save a verified forecast for GW${next.gameweek}`, 'The next deadline has no verified pre-deadline forecast. Capture predictions and the Scout shortlist before it closes.', 'capture-form');
    }
    if (report.runtime?.server_errors > 0) {
      add('runtime', 'Review', 'Investigate recorded server errors', `${report.runtime.server_errors} HTTP 5xx responses since this process started. Check service logs; this cumulative count does not establish an ongoing outage.`, 'runtime');
    }
    return items.sort((a, b) => ({Review: 0, 'Next step': 1, Improve: 2}[a.priority] - {Review: 0, 'Next step': 1, Improve: 2}[b.priority]));
  }
  function freshness(report, now = Date.now()) {
    const week = report.selected || {}, metadata = week.metadata || {}, enrichment = metadata.enrichment || {};
    const stamp = (id, label, timestamp, status, detail) => {
      const parsed = timestamp ? Date.parse(timestamp) : NaN;
      return {id, label, timestamp: Number.isFinite(parsed) ? timestamp : null,
        age_seconds: Number.isFinite(parsed) ? (now - parsed) / 1000 : null, status, detail};
    };
    return [
      stamp('forecast', week.replay ? 'Retrospective estimate' : 'Forecast saved', week.replay?.generated_at_utc || week.captured_at_utc,
        week.forecast_state || 'missing', 'Selected gameweek. Refresh does not run inference.'),
      stamp('results', 'Official results fetched', week.actuals_at_utc, week.result_state || 'unknown',
        week.result_state === 'final' ? 'Final results are retained; their age does not make them stale.' : 'Provisional or missing results may change. The report can reuse cached official data.'),
      stamp('enrichment', 'Enrichment recorded', enrichment.status ? metadata.captured_at_utc : null, enrichment.status || 'not-recorded',
        `Recorded with this forecast; source update time is not recorded.${enrichment.source_observed_gameweek != null ? ` Source covers through GW${enrichment.source_observed_gameweek}; required through GW${enrichment.required_history_gameweek ?? '—'}.` : ''}`),
    ];
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {suggestions, freshness};
  else window.OpenFPLHealth = {suggestions, freshness};
})();
