/* OpenFPL Observatory: private data and credentials stay in this tab's memory. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const fmt = (v, digits = 2) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('en-GB', {minimumFractionDigits: digits, maximumFractionDigits: digits});
  const date = v => v ? new Date(v).toLocaleString('en-GB', {dateStyle: 'medium', timeStyle: 'short'}) : 'Not recorded';
  const human = v => String(v || 'Not recorded').replaceAll('_', ' ').replaceAll('-', ' ');
  const percent = v => v == null ? '—' : `${fmt(v, 1)}%`;
  const state = {key: '', report: null, training: null, tab: 'overview', page: 0, epoch: 0, pending: new Map()};
  const pagesize = 40;
  const views = {
    overview: ['THE BIG PICTURE', 'Season overview'], gameweek: ['EVERY MATCH. EVERY RETURN.', 'Gameweek centre'],
    decisions: ['THE SYSTEM IN THE MANAGER’S SEAT', 'Manager decisions'], players: ['THE PEOPLE BEHIND THE POINTS', 'Player intelligence'],
    models: ['PUT THE MODELS TO THE TEST', 'Model lab'], system: ['THE ENGINE BEHIND THE CALLS', 'System health'],
  };
  function el(tag, text, cls) { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; }
  function value(v, cls = '', digits = 2) { return el('span', fmt(v, digits), cls); }
  function signed(v) { return v == null ? '—' : `${v > 0 ? '+' : ''}${fmt(v)}`; }
  function note(text) { return el('p', text, 'fine'); }
  function empty(id, text) { $(id).replaceChildren(el('p', text, 'empty')); }
  function message(text) { $('message').textContent = text; $('message').hidden = !text; }
  function badge(id, text, good = false) { $(id).textContent = text; $(id).className = `badge ${good ? 'good' : 'warn'}`; }
  function metric(label, score, detail, tone = '') { const n = el('article', null, 'metric'); n.append(el('div', label, 'label'), el('div', score, `value ${tone}`), el('p', detail)); return n; }
  function options(id, items, selected) { $(id).replaceChildren(...items.map(([v, label]) => {const n = el('option', label); n.value = v; n.selected = String(v) === String(selected); return n;})); }
  function table(id, headers, rows) {
    const t = el('table'), h = el('thead'), tr = el('tr'), body = el('tbody');
    headers.forEach(label => { const th = el('th', label); th.scope = 'col'; tr.append(th); }); h.append(tr);
    rows.forEach(cells => { const row = el('tr'); cells.forEach(v => {const cell = el('td'); cell.append(v instanceof Node ? v : document.createTextNode(v == null ? '—' : String(v))); row.append(cell);}); body.append(row); });
    if (!rows.length) { const row = el('tr'), cell = el('td', 'No comparable records in this view.', 'empty'); cell.colSpan = headers.length; row.append(cell); body.append(row); }
    t.append(h, body); $(id).replaceChildren(t);
  }
  function playerLink(p) { const n = el('button', p.name, 'link-button'); n.type = 'button'; n.addEventListener('click', () => openPlayer(p.id)); return n; }
  function playerName(p) { const n = el('div'); n.append(playerLink(p), el('small', p.team || '')); return n; }
  function currentAnalytics() { return state.report.analytics[$('evaluation-mode').value === 'verified' ? 'verified' : 'all']; }
  async function api(path, {channel = 'main', method = 'GET', timeout = 90000} = {}) {
    state.pending.get(channel)?.controller.abort();
    const controller = new AbortController(), epoch = state.epoch, ticket = {controller};
    state.pending.set(channel, ticket);
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {method, headers: {Authorization: `Bearer ${state.key}`}, cache: 'no-store', credentials: 'omit', signal: controller.signal});
      if (epoch !== state.epoch || state.pending.get(channel) !== ticket) return null;
      if (response.status === 401) { lock(); message('Your owner key was not accepted. Enter the dedicated dashboard key.'); return null; }
      const body = await response.json().catch(() => ({}));
      if (epoch !== state.epoch || state.pending.get(channel) !== ticket) return null;
      if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `The request failed (${response.status}).`);
      return body;
    } catch (e) {
      if (epoch !== state.epoch || state.pending.get(channel) !== ticket) return null;
      throw new Error(e.name === 'AbortError' ? 'The request timed out. Please retry.' : e.message);
    } finally { clearTimeout(timer); if (state.pending.get(channel) === ticket) state.pending.delete(channel); }
  }
  function lock() {
    state.epoch++; state.key = ''; state.report = null; state.training = null; state.page = 0;
    state.pending.forEach(t => t.controller.abort()); state.pending.clear();
    $('owner-key').value = ''; $('dashboard').hidden = true; $('login').hidden = false; $('lock').hidden = true;
    $('player-dialog').close();
    ['summary','season-pair','season-chart','trend','week-map','insights','season-clubs','week-overview','fixtures','football-stats','our-leaders','actual-leaders','ranking-metrics','scatter','gw-calibration','underperform','overperform','positions','week-clubs','decision-metrics','pitch','bench','captain-audit','hindsight','decision-bars','decision-history','players','season-players','live-models','training-metrics','training-table','training-scatter','training-calibration','training-trend','folds','training-features','pipeline','model-status','feature-coverage','archive-ledger','runtime','warnings','player-detail-metrics','player-history-chart','player-history-table','player-components'].forEach(id => $(id).replaceChildren());
    ['season','gameweek','training-model','capture-gameweek'].forEach(id => $(id).replaceChildren());
    ['search','player-title','player-subtitle','capture-result','training-context','evaluation-context','updated','season-population','season-bias','week-context','result-badge','fixture-progress','decision-context','formation-title','player-count','page-count','official-badge','model-context','runtime-scope'].forEach(id => {if ($(id).tagName === 'INPUT') $(id).value = ''; else $(id).textContent = '';});
    $('evaluation-mode').value = 'all'; $('scope').value = 'all'; $('position').value = '';
    $('unlock').disabled = false; $('refresh').disabled = false; $('capture').disabled = false;
    $('dashboard').classList.remove('loading'); setTab('overview'); message(''); $('owner-key').focus();
  }
  async function refresh(initial = false) {
    if (!state.key) return;
    const epoch = state.epoch, query = new URLSearchParams();
    if (!initial && $('season').value) query.set('season', $('season').value);
    if (!initial && $('gameweek').value) query.set('gameweek', $('gameweek').value);
    $('unlock').disabled = true; $('refresh').disabled = true; $('dashboard').classList.add('loading');
    message(initial ? 'Opening your observatory…' : 'Refreshing archived estimates and official results…');
    try {
      const data = await api(`/api/admin/dashboard?${query}`);
      if (!data) return;
      state.report = data; render();
      $('login').hidden = true; $('dashboard').hidden = false; $('lock').hidden = false; message('');
    } catch(e) { message(`${e.message}${state.report ? ' The previous snapshot remains visible.' : ''}`); if (initial) state.key = ''; }
    finally { if (epoch === state.epoch) { $('unlock').disabled = false; $('refresh').disabled = false; $('dashboard').classList.remove('loading'); } }
  }
  function setTab(tab, focus = false) {
    state.tab = tab;
    Object.keys(views).forEach(name => { $(`view-${name}`).hidden = name !== tab; const b = $(`tab-${name}`); b.setAttribute('aria-selected', String(name === tab)); b.tabIndex = name === tab ? 0 : -1; });
    $('view-eyebrow').textContent = views[tab][0]; $('view-title').replaceChildren(document.createTextNode(views[tab][1]), el('span', '.', 'lime'));
    if (focus) $(`tab-${tab}`).focus();
    if (tab === 'models' && state.report && !state.training && !state.pending.has('models')) loadTraining();
  }
  function render() {
    const r = state.report, w = r.selected;
    options('season', r.seasons.map(s => [s, s.replace('-', ' / ')]), r.season);
    options('gameweek', r.gameweeks.map(g => [g.gameweek, `GW ${g.gameweek}${g.prediction_count ? '' : ' · no forecast'}`]), w?.gameweek);
    $('updated').textContent = `Snapshot ${date(r.generated_at_utc)} · Times in your timezone`;
    $('warnings').replaceChildren(...r.warnings.map(note)); $('warnings').hidden = !r.warnings.length;
    const verified = $('evaluation-mode').value === 'verified', summary = verified ? r.summary : r.comparison_summary;
    $('evaluation-context').textContent = verified
      ? `${summary.evaluated_gameweeks} completed gameweeks with pre-deadline points forecasts. Other saved runs remain available in the gameweek review.`
      : `${summary.evaluated_gameweeks} completed gameweeks with points forecasts · ${r.comparison_summary.post_deadline_gameweeks} late runs. Retrospective comparisons are not proof of advance accuracy.`;
    renderOverview(); renderGameweek(); renderDecisions(); renderPlayers(); renderLiveModels(); renderSystem();
    if (state.tab === 'models' && !state.training && !state.pending.has('models')) loadTraining();
  }
  function renderOverview() {
    const r = state.report, a = currentAnalytics(), m = a.metrics, weeks = a.timeline.filter(w => w.count);
    $('season-population').textContent = `${fmt(m.count,0)} matched player-gameweek forecasts across ${weeks.length} completed gameweeks. Points estimates and official returns are compared on exactly the same rows.`;
    const pair = (label, v, cls) => { const n = el('div'); n.append(el('small', label), el('strong', fmt(v), cls), el('small', 'points / player / GW')); return n; };
    $('season-pair').replaceChildren(pair('OUR MEAN FORECAST',m.predicted_mean,'ours'),el('span','vs','versus'),pair('OFFICIAL MEAN RETURN',m.actual_mean,'actual'));
    $('season-bias').textContent = m.bias == null ? 'No finalized points comparisons in this evidence scope yet.' : `We ${m.bias >= 0 ? 'overpredicted' : 'underpredicted'} by ${fmt(Math.abs(m.bias))} points per matched player on average.`;
    $('summary').replaceChildren(metric('MEAN ABSOLUTE ERROR',fmt(m.mae),'Average distance from the official score','ours'),metric('ROOT MEAN SQUARED ERROR',fmt(m.rmse),'Larger misses receive more weight'),metric('WITHIN TWO POINTS',percent(m.within_two_pct),'Share of matched points forecasts','actual'),metric('RANK CORRELATION',fmt(m.rank_correlation),'Spearman · −1 to +1; higher is better'));
    lineChart('season-chart',weeks,'gameweek',[['predicted_mean','Our mean forecast','ours'],['actual_mean','Official mean return','actual']],{label:'Mean predicted and actual points by gameweek',tick:w=>`GW${w.gameweek}`,click:w=>selectWeek(w.gameweek)});
    $('trend-context').textContent = 'Mean absolute error in points. Includes only final points comparisons within the selected evidence scope.';
    lineChart('trend',weeks,'gameweek',[['mae','Mean absolute error','ours']],{label:'Prediction error by gameweek',tick:w=>`GW${w.gameweek}`,click:w=>selectWeek(w.gameweek)});
    $('week-map').replaceChildren(...r.gameweeks.map(w=>{const b=el('button',String(w.gameweek));b.type='button';b.dataset.state=!w.prediction_count?'missing':w.result_state==='final'?(w.eligible?'final':'retrospective'):w.result_state==='awaiting-results'?'upcoming':'retrospective';b.classList.toggle('active',w.gameweek===r.selected?.gameweek);b.title=`GW${w.gameweek} · ${human(w.forecast_state)} · ${human(w.result_state)}`;b.setAttribute('aria-label',b.title);b.addEventListener('click',()=>selectWeek(w.gameweek));return b;}));
    const best = [...a.clubs].filter(x=>x.count).sort((x,y)=>x.mae-y.mae)[0];
    const hardest = [...a.positions].filter(x=>x.count).sort((x,y)=>y.mae-x.mae)[0];
    const latest = r.gameweeks.filter(w=>w.eligible&&w.result_state==='awaiting-results').slice(-1)[0];
    const insights = [ ['Most closely tracked club',best?`${best.name} · ${fmt(best.mae)} MAE across ${best.count} matched rows.`:'Club comparisons will appear with matched results.'],['Largest positional error',hardest?`${hardest.name} · ${fmt(hardest.predicted_mean)} predicted vs ${fmt(hardest.actual_mean)} actual points per player.`:'No positional comparison in this scope yet.'],['The next evidence point',latest?`GW${latest.gameweek} has a preserved pre-deadline forecast. Its results will enter verified accuracy once finalized.`:'Save an upcoming forecast in System health to build verified evidence.'] ];
    $('insights').replaceChildren(...insights.map(([title,copy])=>{const card=el('article',null,'panel');card.append(el('h3',title),el('p',copy));return card;}));
    comparisonTable('season-clubs',a.clubs,'Club');
  }
  function comparisonTable(id, rows, label = 'Group') { table(id,[label,'Matched','Our pts','Actual pts','MAE','Bias'],rows.filter(r=>r.count).map(r=>[r.name,fmt(r.count,0),value(r.predicted_total,'ours'),value(r.actual_total,'actual'),fmt(r.mae),signed(r.bias)])); }
  async function selectWeek(gw) { $('gameweek').value=gw;state.page=0;setTab('gameweek');await refresh(); }
  function leaderboard(id, players, limit = 7) { table(id,['Player','Our pts / score','Actual','Minutes'],players.slice(0,limit).map(p=>[playerName(p),p.expected_points==null?el('span',`Score ${fmt(p.selection_score)}`,'ours'):value(p.expected_points,'ours'),value(p.actual_points,'actual',0),fmt(p.minutes,0)])); }
  function renderGameweek() {
    const w = state.report.selected, a = w?.analysis;
    badge('result-badge',human(w?.result_state),w?.result_state==='final');
    $('week-context').textContent = w ? `GW${w.gameweek} · ${w.is_points_forecast?'Points forecast':'Ownership ranking, not a points forecast'} · ${human(w.forecast_state)} · saved ${date(w.captured_at_utc)} · deadline ${date(w.deadline_time)} · results observed ${date(w.actuals_at_utc)}` : 'No archived gameweek yet.';
    const m=w?.metrics||{};
    $('week-overview').replaceChildren(metric('OUR MEAN POINTS',fmt(m.predicted_mean),'Matched points forecasts only','ours'),metric('ACTUAL MEAN POINTS',fmt(m.actual_mean),'Same matched player population','actual'),metric('ACTUAL SCORES MATCHED',`${w?.matched_actuals||0} / ${w?.prediction_count||0}`,'Missing results remain unknown'),metric('OFFICIAL MANAGER AVERAGE',fmt(a?.average_manager_score,0),'Official FPL score, not a player average','actual'));
    badge('fixture-progress',`${a?.finished_fixtures||0} / ${a?.fixture_count||0} finished`,!!a?.fixture_count && a.finished_fixtures===a.fixture_count);
    $('fixtures').replaceChildren(...(a?.fixtures||[]).map(f=>{const card=el('article',null,'fixture'),score=el('div',null,'fixture-score');score.append(el('span',f.home),el('strong',`${fmt(f.home_score,0)} : ${fmt(f.away_score,0)}`),el('span',f.away));card.append(score,el('p',`${f.finished?'Full time':f.started?'In progress':'Upcoming'} · ${date(f.kickoff_time)} · FDR ${f.home_difficulty??'—'} / ${f.away_difficulty??'—'}`));return card;}));
    if(!a?.fixtures.length) empty('fixtures','No official fixtures available for this gameweek.');
    $('football-stats').replaceChildren(...(a?.official_totals||[]).map(s=>{const n=el('div');n.append(el('strong',fmt(s.actual,0)),el('small',human(s.name)));return n;}));
    leaderboard('our-leaders',a?.selection.our_top||[]);leaderboard('actual-leaders',a?.selection.actual_top||[]);
    const ranking=a?.selection||{};
    $('ranking-metrics').replaceChildren(metric('TOP-10 OVERLAP',percent(ranking.top10_overlap_pct),'Our top ten vs actual top ten in the scored pool'),metric('TOP-10 HAUL RATE',percent(ranking.haul_rate_pct),'Share of our top ten returning 6+ points'),metric('RANKING QUALITY',fmt(ranking.ndcg),'NDCG@10 · actual-point gain; 1 is ideal'),metric('OUR TOP-10 ACTUAL RETURN',fmt(ranking.chosen_actual,0),`Actual top ten returned ${fmt(ranking.best_actual,0)} points`,'actual'));
    scatterChart('scatter',w?.players||[],'All matched player forecasts vs official points');
    pairedBars('gw-calibration',a?.calibration||[],'band','predicted_mean','actual_mean');
    leaderboard('underperform',a?.biggest_under||[],5);leaderboard('overperform',a?.biggest_over||[],5);
    comparisonTable('positions',(w?.positions||[]).map(p=>({...p,name:p.position})),'Position');comparisonTable('week-clubs',a?.clubs||[],'Club');
  }
  function renderDecisions() {
    const w=state.report.selected,a=w?.analysis,d=a?.squad||{},xi=d.xi||[];
    $('decision-context').textContent=`${d.note||''} ${w?`GW${w.gameweek}: ${human(w.forecast_state)}. Results are ${human(w.result_state)}.`:''}`;
    $('decision-metrics').replaceChildren(metric('DERIVED XI · PREDICTED',fmt(d.predicted_points),'Captain doubled · no chips','ours'),metric('SAME XI · ACTUAL',fmt(d.actual_points),'Fixed XI and captain · no autosubs','actual'),metric('OFFICIAL MANAGER AVERAGE',fmt(a?.average_manager_score,0),'Context only: managers have budgets, autosubs and chips'),metric('SAME SQUAD · HINDSIGHT',fmt(d.hindsight_points),'Best legal XI and captain using actual results'));
    const formation=['DEF','MID','FWD'].map(pos=>xi.filter(p=>p.position===pos).length).join('–');
    $('formation-title').textContent=xi.length?`The ${formation} selection`:'No complete saved shortlist';
    const card = p => {const b=el('button',null,'pitch-player');b.type='button';const name=el('span',p.name,'name');if(p.is_captain)name.append(el('span','C','captain-chip'));const nums=el('div',null,'pitch-points');nums.append(value(p.expected_points,'ours',1),value(p.actual_points,'actual',0));b.append(el('small',p.position),name,nums);b.title=`${p.name}: ${fmt(p.expected_points)} predicted, ${fmt(p.actual_points,0)} actual`;b.addEventListener('click',()=>openPlayer(p.id));return b;};
    $('pitch').replaceChildren(...['GK','DEF','MID','FWD'].map(pos=>{const row=el('div',null,'pitch-row');row.append(...xi.filter(p=>p.position===pos).map(card));return row;}));
    if(!xi.length) empty('pitch','A complete saved 15-player shortlist is needed to derive a legal starting XI.');
    $('bench').replaceChildren(...(d.bench||[]).map(card));
    const captain=xi.find(p=>p.is_captain),best=xi.filter(p=>p.actual_points!=null).sort((x,y)=>y.actual_points-x.actual_points)[0];
    $('captain-audit').replaceChildren();
    if(captain){$('captain-audit').append(playerLink(captain));[['Predicted base points',captain.expected_points,'ours'],['Actual base points',captain.actual_points,'actual'],['Extra captain points',captain.actual_points,'actual']].forEach(([label,v,tone])=>{const row=el('div',null,'pair-line');row.append(el('span',label),el('strong',fmt(v),tone));$('captain-audit').append(row);});if(best)$('captain-audit').append(note(`Highest actual return in this XI: ${best.name}, ${fmt(best.actual_points,0)} points. Captain values shown before chips or vice-captain fallback.`));}else empty('captain-audit','No captain comparison is available.');
    $('hindsight').replaceChildren(metric('SELECTION OPPORTUNITY',fmt(d.selection_gap),'Hindsight XI and captain − our fixed XI and captain','actual'));
    const benchTotal=(d.bench||[]).length&&(d.bench||[]).every(p=>p.actual_points!=null)?d.bench.reduce((sum,p)=>sum+p.actual_points,0):null;
    $('hindsight').append(note(`Raw bench returns: ${fmt(benchTotal,0)} points. Bench points are not all recoverable within formation rules.`));
    pairedBars('decision-bars',xi,'name','expected_points','actual_points');
    table('decision-history',['GW','Evidence','Our XI pts','Actual XI pts','FPL average','Hindsight','Opportunity'],currentAnalytics().decisions.map(d=>[gwButton(d.gameweek),human(d.forecast_state),value(d.predicted_points,'ours'),value(d.actual_points,'actual'),fmt(d.official_average,0),fmt(d.hindsight_points),fmt(d.selection_gap)]));
  }
  function gwButton(gw){const b=el('button',`GW${gw}`,'link-button');b.type='button';b.addEventListener('click',()=>selectWeek(gw));return b;}
  function filteredPlayers() {
    const q=$('search').value.trim().toLocaleLowerCase(),pos=$('position').value,scope=$('scope').value,sort=$('sort').value;
    return (state.report?.selected?.players||[]).filter(p=>(!q||`${p.name} ${p.team}`.toLocaleLowerCase().includes(q))&&(!pos||p.position===pos)&&(scope!=='squad'||p.in_squad)&&(scope!=='matched'||p.actual_points!=null)&&(scope!=='missing'||p.actual_points==null)&&(scope!=='played'||p.minutes>=60)).sort((a,b)=>{if(sort==='name')return a.name.localeCompare(b.name);const v=p=>sort==='expected_points'?(p.expected_points??p.selection_score??-Infinity):p[sort]==null?-Infinity:sort==='error'?Math.abs(p.error):p[sort];return v(b)-v(a)||a.name.localeCompare(b.name);});
  }
  function renderPlayers(){
    const players=filteredPlayers(),pageCount=Math.max(1,Math.ceil(players.length/pagesize));state.page=Math.min(state.page,pageCount-1);
    $('player-count').textContent=`${players.length} players in this view · GW${state.report?.selected?.gameweek||'—'} · select a name for the full season dossier`;
    $('players').replaceChildren(...players.slice(state.page*pagesize,(state.page+1)*pagesize).map(p=>{const row=el('tr');[playerName(p),p.position,p.expected_points==null?el('span',`Score ${fmt(p.selection_score)}`,'ours'):value(p.expected_points,'ours'),value(p.actual_points,'actual',0),el('span',signed(p.error),p.error>0?'over':'under'),fmt(p.minutes,0),fmt(p.goals,0),fmt(p.assists,0),fmt(p.bonus,0),human(p.role|| (p.in_squad?'shortlist':'—'))].forEach(v=>{const cell=el('td');cell.append(v instanceof Node?v:document.createTextNode(v));row.append(cell);});return row;}));
    if(!players.length){const row=el('tr'),cell=el('td','No players match these filters.','empty');cell.colSpan=10;row.append(cell);$('players').append(row);}
    $('page-count').textContent=`Page ${state.page+1} of ${pageCount}`;$('previous-page').disabled=state.page===0;$('next-page').disabled=state.page>=pageCount-1;$('export').disabled=!players.length;
    table('season-players',['Player','GWs','Matched','Our pts','Actual pts','MAE','Bias'],currentAnalytics().players.filter(p=>p.count).slice(0,20).map(p=>[playerName(p),p.gameweeks,p.count,value(p.predicted_total,'ours'),value(p.actual_total,'actual'),fmt(p.mae),signed(p.bias)]));
  }
  function modelTable(id, models) {
    table(id,['Model / baseline','Matched','Our mean','Actual mean','MAE ↓','RMSE ↓','Bias','R²','Rank ρ'],models.map(m=>[human(m.name),fmt(m.count,0),value(m.predicted_mean,'ours'),value(m.actual_mean,'actual'),fmt(m.mae),fmt(m.rmse),signed(m.bias),fmt(m.r2),fmt(m.rank_correlation)]));
  }
  function renderLiveModels() {
    const w=state.report.selected, isWeek=$('live-model-window').value==='gameweek';
    const models=isWeek?(w?.result_state==='final'?w.analysis.models:[]):currentAnalytics().models;
    modelTable('live-models',models);
    if(isWeek&&w?.result_state!=='final') $('live-models').append(note('This gameweek has no finalized results yet. Saved model estimates are available in each player dossier.'));
    else if(!models.some(m=>m.name!=='ensemble'&&m.count)) $('live-models').append(note('Individual model outputs were not recorded for these completed gameweeks. New forecast captures retain them for future comparisons.'));
  }
  async function loadTraining() {
    if(!state.key)return;
    const epoch=state.epoch;
    $('reload-models').disabled=true;$('training-context').textContent='Reading recorded model evaluations…';state.training=null;
    ['training-metrics','training-table','training-scatter','training-calibration','training-trend','folds','training-features'].forEach(id=>$(id).replaceChildren());
    options('training-model',[],null);
    try {const r=await api(`/api/admin/models?dataset=${encodeURIComponent($('training-dataset').value)}`,{channel:'models'});if(!r)return;state.training=r;renderTraining();}
    catch(e){$('training-context').textContent=e.message;}
    finally{if(epoch===state.epoch)$('reload-models').disabled=false;}
  }
  function renderTraining() {
    const r=state.training;if(!r)return;
    if(!r.available){$('training-context').textContent=r.message;return;}
    $('training-context').textContent=`${r.note} Trained ${date(r.trained_at_utc)}. Evaluation season labels: ${r.evaluation_seasons.join(', ')}. ${r.warnings.join(' ')}`;
    const best=r.models.find(m=>m.count),ensemble=r.models.find(m=>m.name==='weighted_ensemble'),baseline=r.models.find(m=>m.kind==='baseline'&&m.count);
    const lift=ensemble&&baseline&&baseline.mae>0?100*(baseline.mae-ensemble.mae)/baseline.mae:null;
    $('training-metrics').replaceChildren(metric('EVALUATION ROWS',fmt(r.rows,0),`${human(r.dataset)} · ${human(r.validation_strategy)}`),metric('LOWEST RECORDED MAE',fmt(best?.mae),human(best?.name),'ours'),metric('ENSEMBLE VS BEST BASELINE',percent(lift),'MAE reduction; positive means ensemble improves'),metric('MODEL INPUTS',fmt(r.feature_count,0),`Dataset season labels: ${r.dataset_seasons.join(', ')}`));
    modelTable('training-table',r.models);
    options('training-model',r.models.map(m=>[m.name,human(m.name)]),ensemble?.name||best?.name);
    $('training-features').replaceChildren(...r.features.map(f=>el('span',human(f))));
    renderTrainingModel();
  }
  function renderTrainingModel(){
    const r=state.training,m=r?.models.find(m=>m.name===$('training-model').value);if(!m)return;
    scatterChart('training-scatter',m.scatter,`${human(m.name)} recorded predictions and labels`);
    pairedBars('training-calibration',m.calibration,'band','predicted_mean','actual_mean');
    lineChart('training-trend',m.timeline,'gameweek',[['mae','Mean absolute error','ours']],{label:'Recorded error by training season and gameweek',tick:w=>`${w.season} / ${w.gameweek}`});
    const folds=r.folds.filter(f=>f.model===m.name);
    table('folds',['Model','Fold','Training window','Validation window','Train rows','Valid rows','MAE','RMSE'],folds.map(f=>[human(f.model),f.fold,`${f.train_period_start} → ${f.train_period_end}`,`${f.validation_period_start} → ${f.validation_period_end}`,fmt(f.train_rows,0),fmt(f.validation_rows,0),fmt(f.mae),fmt(f.rmse)]));
    if(!folds.length)$('folds').append(note('Per-fold training records are available for component models. Choose a component above to inspect its validation windows.'));
  }
  function renderSystem(){
    const r=state.report,s=r.system,t=r.runtime||{};
    badge('official-badge',`Official FPL ${r.official_status}`,r.official_status==='available');
    const pipeline=[['OFFICIAL SOURCE',human(r.official_status),'Official event totals and fixture scores'],['PREDICTION ARCHIVE',human(s.archive.last_result?.status|| (s.archive.enabled?'Enabled':'Disabled')),`${r.comparison_summary.archived_gameweeks} saved gameweeks in ${r.season||'this season'}`],['OPTIONAL ENRICHMENT',human(s.enrichment.status|| (s.enrichment_enabled?'Enabled':'Disabled')),'Historical statistics supplement the official source']];
    $('pipeline').replaceChildren(...pipeline.map(([title,status,copy])=>{const n=el('article');n.append(el('h3',title),el('strong',status),el('p',copy));return n;}));
    table('model-status',['Model','Artifact','Version','Last inference','Weight','Diagnostic'],s.models.map(m=>[human(m.name),m.loaded?'Loaded':'Missing',m.version||'—',human(m.last_inference),m.weight==null?'—':percent(m.weight*100),m.error||'—']));
    $('model-context').textContent=`Latest saved inference: GW${s.last_inference_gameweek||'—'} · ${date(s.last_inference_at)} · ${human(s.strategy)} · mean model spread ${fmt(s.mean_model_spread)}. Artifact loading reflects this running instance; inference status reflects the saved run.`;
    const coverage=s.feature_coverage;
    $('feature-coverage').replaceChildren();
    if(coverage){[['populated','present'],['entirely_missing','missing']].forEach(([field,status])=>(coverage[field]||[]).forEach(f=>{const tag=el('span',`${status==='present'?'✓':'!'} ${human(f)}`);tag.dataset.status=status;$('feature-coverage').append(tag);}));}
    else empty('feature-coverage','Feature-level coverage was not recorded for this inference.');
    const selected=$('capture-gameweek').value,upcoming=r.gameweeks.filter(w=>w.deadline_time&&Date.parse(w.deadline_time)>Date.now());
    options('capture-gameweek',upcoming.map(w=>[w.gameweek,`GW ${w.gameweek} · ${date(w.deadline_time)}`]),upcoming.some(w=>String(w.gameweek)===selected)?selected:upcoming[0]?.gameweek);
    $('capture').disabled=!upcoming.length||state.pending.has('capture');
    if(!upcoming.length)$('capture-result').textContent='No upcoming deadline in the selected season. Select the current season to capture a forecast.';
    table('archive-ledger',['GW','Forecast','Saved at','Official deadline','Results','Matched','Points MAE'],r.gameweeks.map(w=>[gwButton(w.gameweek),human(w.forecast_state),w.captured_at_utc?date(w.captured_at_utc):'—',date(w.deadline_time),human(w.result_state),`${w.matched_actuals} / ${w.prediction_count}`,fmt(w.metrics.mae)]));
    $('runtime').replaceChildren(metric('UPTIME',`${fmt((t.uptime_seconds||0)/3600,1)} h`,'Since this process started'),metric('REQUESTS',fmt(t.requests,0),'Excludes dashboard activity'),metric('SERVER ERRORS',fmt(t.server_errors,0),'HTTP 5xx responses'),metric('P95 LATENCY',`${fmt(t.p95_latency_ms,0)} ms`,`${t.latency_sample_size||0} recent request samples`));
    $('runtime-scope').textContent=t.scope||'';
  }
  async function captureForecast(event){
    event.preventDefault();if(state.pending.has('capture')||!state.key)return;
    const gw=$('capture-gameweek').value,epoch=state.epoch;if(!gw)return;
    $('capture').disabled=true;$('capture-result').textContent=`Capturing GW${gw} forecasts and squad decisions…`;
    try{
      const r=await api(`/api/admin/capture?gameweek=${encodeURIComponent(gw)}`,{method:'POST',channel:'capture',timeout:180000});if(!r)return;
      const ok=r.archive?.status==='saved'&&r.squad?.status==='saved';
      $('capture-result').textContent=ok?`GW${r.gameweek} forecast, model outputs and shortlist saved. The archive records their actual capture time.`:`Capture incomplete: predictions ${human(r.archive?.status)}, squad ${human(r.squad?.status)}. ${r.archive?.error||r.squad?.error||'Check archive configuration in the service.'}`;
      await refresh();
    }catch(e){if(epoch===state.epoch)$('capture-result').textContent=e.message;}
    finally{if(epoch===state.epoch)$('capture').disabled=false;}
  }
  async function openPlayer(id){
    const dialog=$('player-dialog');$('player-title').textContent='Loading player…';$('player-subtitle').textContent='Reading saved forecasts and official returns.';
    ['player-detail-metrics','player-history-chart','player-history-table','player-components'].forEach(id=>$(id).replaceChildren());
    if(!dialog.open)dialog.showModal();
    try{
      const data=await api(`/api/admin/players/${id}?season=${encodeURIComponent(state.report.season)}`,{channel:'player'});if(!data||!dialog.open)return;
      const p=data.player,m=data.metrics;
      $('player-title').textContent=p.name;
      $('player-subtitle').textContent=`${p.team} · ${p.position} · ${data.season} · ${p.price==null?'Price not archived':`£${fmt(p.price,1)}m`} · ${p.ownership==null?'Ownership not archived':`${percent(p.ownership)} ownership`}. Profile context is from the latest saved forecast. Metrics include final points comparisons across all archived runs, including late runs.`;
      $('player-detail-metrics').replaceChildren(metric('OUR MATCHED POINTS',fmt(m.predicted_total),`${m.count} player-gameweek points forecasts`,'ours'),metric('ACTUAL MATCHED POINTS',fmt(m.actual_total),'Same matched gameweeks','actual'),metric('MEAN ABSOLUTE ERROR',fmt(m.mae),'Ownership scores excluded'),metric('MEAN BIAS',signed(m.bias),'Predicted minus actual'));
      lineChart('player-history-chart',data.history,'gameweek',[['expected_points','Our points','ours'],['actual_points','Actual points','actual']],{label:`${p.name}: predicted and actual points`,tick:w=>`GW${w.gameweek}`});
      table('player-history-table',['GW','Evidence','Results','Our points / score','Actual','Minutes','Goals','Assists','Bonus'],data.history.map(w=>[w.gameweek,human(w.forecast_state),human(w.result_state),w.expected_points==null?`Score ${fmt(w.selection_score)}`:value(w.expected_points,'ours'),value(w.actual_points,'actual',0),fmt(w.minutes,0),fmt(w.goals,0),fmt(w.assists,0),fmt(w.bonus,0)]));
      const components=data.history.flatMap(w=>Object.entries(w.model_predictions||{}).map(([name,prediction])=>[w.gameweek,human(name),value(prediction,'ours'),value(w.actual_points,'actual',0),signed(prediction!=null&&w.actual_points!=null?prediction-w.actual_points:null)]));
      table('player-components',['GW','Saved model component','Our points','Actual','Error'],components);
      if(!components.length)$('player-components').append(note('Individual model predictions begin with new forecast captures.'));
    }catch(e){if(state.key&&dialog.open)$('player-subtitle').textContent=e.message;}
  }

  // SVG charts are local, keyboard accessible and use the same rows as the tables.
  function svgNode(tag,attrs={},text){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));if(text!=null)n.textContent=text;return n;}
  function chart(id,label){const svg=svgNode('svg',{viewBox:'0 0 620 240',role:'img','aria-label':label});svg.append(svgNode('title',{},label));$(id).replaceChildren(svg);return svg;}
  function chartRange(values){const valid=values.filter(v=>v!=null&&Number.isFinite(v));let lo=Math.min(0,...valid),hi=Math.max(1,...valid);const pad=(hi-lo)*.07;return [lo<0?lo-pad:lo,hi+pad];}
  function yAxis(svg,lo,hi){const y=v=>200-(v-lo)/(hi-lo)*180;for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4;svg.append(svgNode('line',{x1:46,x2:604,y1:y(v),y2:y(v),class:'grid-line'}),svgNode('text',{x:38,y:y(v)+3,'text-anchor':'end'},fmt(v,1)));}return y;}
  function chartTip(node,text,action){node.append(svgNode('title',{},text));if(action){node.setAttribute('role','button');node.setAttribute('tabindex','0');node.setAttribute('aria-label',text);node.addEventListener('click',action);node.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();action();}});}}
  function lineChart(id,rows,xfield,series,{label='Predicted and actual points',tick,click}={}){
    if(!rows.length||!rows.some(r=>series.some(([field])=>r[field]!=null))){empty(id,'No comparable values in this view yet. Check another gameweek or evidence scope.');return;}
    const svg=chart(id,label),[lo,hi]=chartRange(rows.flatMap(r=>series.map(([field])=>r[field]))),y=yAxis(svg,lo,hi),x=i=>rows.length===1?325:54+i/(rows.length-1)*540;
    const stride=Math.max(1,Math.ceil(rows.length/6));
    rows.forEach((row,i)=>{if(i%stride===0||i===rows.length-1)svg.append(svgNode('text',{x:x(i),y:225,'text-anchor':'middle'},tick?tick(row):String(row[xfield])));});
    series.forEach(([field,name,tone])=>{
      let path='',connected=false;
      rows.forEach((r,i)=>{if(r[field]==null){connected=false;return;}path+=`${connected?' L':' M'}${x(i)} ${y(r[field])}`;connected=true;});
      svg.append(svgNode('path',{d:path,class:`series ${tone==='actual'?'actual-series':''}`}));
      rows.forEach((r,i)=>{if(r[field]==null)return;const c=svgNode('circle',{cx:x(i),cy:y(r[field]),r:rows.length>50?2.5:4.5,class:`point ${tone==='actual'?'actual-point':''}`});chartTip(c,`${tick?tick(r):r[xfield]} · ${name}: ${fmt(r[field])}`,click?()=>click(r):null);svg.append(c);});
    });
  }
  function scatterChart(id,rows,label){
    const matched=rows.filter(p=>p.expected_points!=null&&p.actual_points!=null);
    if(!matched.length){empty(id,'No matched points forecasts yet. Ownership rankings are evaluated in the ranking panels.');return;}
    const svg=chart(id,label),[lo,hi]=chartRange(matched.flatMap(p=>[p.expected_points,p.actual_points])),y=yAxis(svg,lo,hi),x=v=>46+(v-lo)/(hi-lo)*558;
    for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4;svg.append(svgNode('text',{x:x(v),y:218,'text-anchor':'middle'},fmt(v,1)));}
    svg.append(svgNode('line',{x1:x(lo),x2:x(hi),y1:y(lo),y2:y(hi),class:'reference'}),svgNode('text',{x:46,y:11},'Actual points ↑'),svgNode('text',{x:325,y:238,'text-anchor':'middle'},'Predicted points →'));
    matched.forEach(p=>{const c=svgNode('circle',{cx:x(p.expected_points),cy:y(p.actual_points),r:p.in_squad?4.5:2.8,class:'scatter-point'});chartTip(c,`${p.name||'Recorded row'} · predicted ${fmt(p.expected_points)}, actual ${fmt(p.actual_points)}`,p.id?()=>openPlayer(p.id):null);svg.append(c);});
  }
  function pairedBars(id,rows,labelField,ours,actual){
    rows=rows.filter(r=>r[ours]!=null||r[actual]!=null);
    if(!rows.length){empty(id,'No predicted and actual values to compare yet.');return;}
    const svg=chart(id,'Lime: our points. Blue: actual points.'),[lo,hi]=chartRange(rows.flatMap(r=>[r[ours],r[actual]])),y=yAxis(svg,lo,hi),slot=548/rows.length,width=Math.min(24,slot*.3);
    rows.forEach((r,i)=>{const cx=52+slot*(i+.5);[[ours,-width,'bar-ours','Our'],[actual,1,'bar-actual','Actual']].forEach(([field,offset,cls,label])=>{if(r[field]==null)return;const b=svgNode('rect',{x:cx+offset,y:Math.min(y(0),y(r[field])),width:width-1,height:Math.max(1,Math.abs(y(r[field])-y(0))),rx:2,class:cls});chartTip(b,`${r[labelField]} · ${label}: ${fmt(r[field])}${r.count!=null?` · ${r.count} rows`:''}`);svg.append(b);});svg.append(svgNode('text',{x:cx,y:222,'text-anchor':'middle'},String(r[labelField]).slice(0,rows.length>8?8:18)));});
  }
  function exportCSV(){
    const w=state.report.selected,rows=filteredPlayers();if(!w||!rows.length)return;
    const fields=['id','name','team','position','expected_points','selection_score','actual_points','error','minutes','goals','assists','bonus','in_squad'];
    const cell=v=>{let s=v==null?'':String(v);if(typeof v==='string'&&/^[=+\-@\t\r]/.test(s))s=`'${s}`;return `"${s.replaceAll('"','""')}"`;};
    const content=[['season','gameweek','forecast_state','result_state','captured_at_utc','deadline_time','actuals_at_utc',...fields],...rows.map(p=>[state.report.season,w.gameweek,w.forecast_state,w.result_state,w.captured_at_utc,w.deadline_time,w.actuals_at_utc,...fields.map(f=>p[f])])].map(row=>row.map(cell).join(',')).join('\r\n');
    const url=URL.createObjectURL(new Blob(['\uFEFF'+content],{type:'text/csv;charset=utf-8'})),link=el('a');link.href=url;link.download=`openfpl-${state.report.season}-gw${w.gameweek}.csv`;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  $('login-form').addEventListener('submit',e=>{e.preventDefault();state.key=$('owner-key').value.trim();$('owner-key').value='';refresh(true);});
  $('lock').addEventListener('click',lock);
  $('refresh').addEventListener('click',()=>refresh());
  $('season').addEventListener('change',()=>{state.page=0;options('gameweek',[],null);$('player-dialog').close();refresh();});
  $('gameweek').addEventListener('change',()=>{state.page=0;refresh();});
  $('evaluation-mode').addEventListener('change',()=>{if(state.report)render();});
  Object.keys(views).forEach(tab=>$(`tab-${tab}`).addEventListener('click',()=>setTab(tab)));
  $('navigation').addEventListener('keydown',e=>{const keys=Object.keys(views),i=keys.indexOf(state.tab);let next;if(['ArrowDown','ArrowRight'].includes(e.key))next=keys[(i+1)%keys.length];if(['ArrowUp','ArrowLeft'].includes(e.key))next=keys[(i-1+keys.length)%keys.length];if(e.key==='Home')next=keys[0];if(e.key==='End')next=keys.at(-1);if(next){e.preventDefault();setTab(next,true);}});
  ['search','position','scope','sort'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',()=>{state.page=0;if(state.report)renderPlayers();}));
  $('previous-page').addEventListener('click',()=>{state.page--;renderPlayers();});$('next-page').addEventListener('click',()=>{state.page++;renderPlayers();});
  $('export').addEventListener('click',exportCSV);
  $('live-model-window').addEventListener('change',()=>{if(state.report)renderLiveModels();});
  $('training-dataset').addEventListener('change',loadTraining);$('reload-models').addEventListener('click',loadTraining);$('training-model').addEventListener('change',renderTrainingModel);
  $('capture-form').addEventListener('submit',captureForecast);
  $('close-player').addEventListener('click',()=>$('player-dialog').close());
  $('player-dialog').addEventListener('close',()=>{state.pending.get('player')?.controller.abort();state.pending.delete('player');});
  setInterval(()=>{if(state.key&&!document.hidden&&!state.pending.has('main')&&!state.pending.has('capture'))refresh();},60000);
  window.addEventListener('pagehide',lock);
})();
