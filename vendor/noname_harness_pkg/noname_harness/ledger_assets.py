"""Self-contained presentation assets shared by live and offline ledgers."""

STYLE = r'''
:root{color-scheme:light;--bg:#f6f5f0;--paper:#fffefa;--ink:#202d27;--muted:#606b63;--line:#dddfd5;--green:#284f3d;--soft:#edf1e9;--orange:#985222;--orange-bg:#fbf0e4;--purple:#6c557d;--serif:"Iowan Old Style","Songti SC","Noto Serif CJK SC",Georgia,serif;--sans:-apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.7 var(--sans);-webkit-font-smoothing:antialiased}a{color:inherit;text-decoration:none}button,input,textarea,select{font:inherit}button,a,input,textarea,select,summary{-webkit-tap-highlight-color:transparent}button:focus-visible,a:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #588064;outline-offset:4px}[hidden]{display:none!important}h1,h2,h3,p,ul,dl{margin:0}ul{padding:0;list-style:none}button{cursor:pointer}button:disabled{cursor:wait;opacity:.6}.skip{position:fixed;top:-100px;left:20px;z-index:8;background:var(--paper);padding:12px}.skip:focus{top:12px}.shell{display:grid;grid-template-columns:230px minmax(0,1fr);min-height:100vh}.sidebar{padding:36px 24px;border-right:1px solid var(--line);position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:30px}.wordmark{font:27px var(--serif);letter-spacing:-1.3px;font-weight:600;display:flex;align-items:center;gap:10px}.mark{display:inline-grid;place-items:center;width:30px;height:30px;background:var(--green);border-radius:9px;color:var(--paper);font:20px var(--serif)}.brand-note{font-size:10px;letter-spacing:1.8px;color:var(--muted);margin:8px 0 0 41px}.project-label,.eyebrow{font-size:10px;letter-spacing:2px;color:var(--muted);font-weight:600}.project-name{font-size:13px;margin-top:6px;overflow-wrap:anywhere}.nav{display:grid;gap:6px}.nav a{display:flex;align-items:center;gap:11px;border-radius:6px;padding:10px 12px;color:var(--muted);font-size:13px}.nav a:hover{background:#e9ece3}.nav a[aria-current=page]{background:#e4eadd;color:var(--green);font-weight:600}.nav-number{font:11px var(--mono);opacity:.65}.nav-count{margin-left:auto;font:11px var(--mono);background:#f4e4d0;color:#8c4c20;border-radius:4px;padding:0 5px}.sidebar-foot{margin-top:auto;font-size:11px;line-height:1.9;color:var(--muted)}.sidebar-foot strong{font-weight:500;color:var(--green)}.sidebar-foot q{display:block;font-family:var(--serif);font-size:14px;margin-top:20px}.main{max-width:1220px;width:100%;padding:30px 52px 60px}.topbar{display:flex;justify-content:space-between;gap:18px;align-items:center;border-bottom:1px solid var(--line);padding-bottom:21px;margin-bottom:38px}.breadcrumb{font-size:12px;color:var(--muted)}.mode{display:inline-flex;align-items:center;gap:7px;font-size:11px;color:var(--green);white-space:nowrap}.mode:before{content:"";width:6px;height:6px;border-radius:50%;background:var(--green)}.searchbar{display:none;gap:6px;align-items:center}.searchbar.ready{display:flex}.searchbar input{max-width:205px;padding:7px 11px;border:1px solid var(--line);border-radius:6px;background:var(--paper);font-size:12px;color:var(--ink)}.searchbar button{border:0;background:transparent;color:var(--muted);font-size:12px;padding:7px}.hero{margin:12px 0 31px}.hero h1{font:36px/1.45 var(--serif);font-weight:500;letter-spacing:-.8px;margin:9px 0 12px}.lede{font-size:13px;line-height:1.9;color:var(--muted);max-width:640px}.page-section{margin-bottom:32px}.page-section>h2{font-size:15px;font-weight:600}.section-heading{display:flex;justify-content:space-between;align-items:baseline;gap:16px;margin:28px 0 13px}.section-heading h2{font-size:15px;font-weight:600}.text-link{font-size:12px;color:var(--green);border-bottom:1px solid #b4c2ae}.current-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}.column-head{display:flex;justify-content:space-between;align-items:baseline;margin:4px 0 12px}.column-head h2{font-size:14px;font-weight:600}.caption,.meta,.muted{color:var(--muted);font-size:12px}.caption{font-size:10px;letter-spacing:.8px}.card{background:var(--paper);border:1px solid var(--line);border-radius:9px;padding:21px;margin-bottom:12px;min-width:0;overflow-wrap:anywhere}.card h3{font-size:14px;font-weight:600;margin:8px 0 11px}.card .prose{font-size:14px;line-height:1.9;white-space:pre-wrap;overflow-wrap:anywhere}.card-foot{margin-top:16px;display:flex;justify-content:space-between;gap:12px;align-items:center;border-top:1px solid #e9ebe2;padding-top:11px}.eyebrow .dot{display:inline-block;width:5px;height:5px;background:var(--green);border-radius:50%;margin-right:7px}.tag,.badge{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:1px 6px;color:var(--muted);font-size:10px;margin-right:6px;vertical-align:middle}.tag.review,.badge.review{color:var(--orange);border-color:#e0c4a5;background:var(--orange-bg)}.tag.taste,.badge.authored{color:var(--purple);border-color:#d9cddd;background:#f5f0f6}.empty{padding:27px 22px;border:1px dashed #cbd1c4;border-radius:8px;color:var(--muted);font-size:13px;line-height:1.9;background:rgba(255,255,255,.22)}.empty strong{display:block;color:var(--ink);font-weight:500;margin-bottom:7px}.quiet-callout{display:flex;justify-content:space-between;gap:22px;align-items:center;border:1px solid #c7d3bf;border-radius:8px;background:#edf1e7;padding:18px 22px;margin:26px 0}.quiet-callout h2{font-size:14px;font-weight:500;margin-bottom:3px}.quiet-callout p{font-size:12px;color:var(--muted)}.button,button.primary{border:1px solid var(--green);background:var(--green);color:#fffefa;border-radius:6px;padding:8px 14px;font-size:12px;display:inline-block;white-space:nowrap}.secondary{border:1px solid #c5ccbe;background:transparent;color:var(--ink);border-radius:6px;padding:8px 12px;font-size:12px}.presence{margin-top:27px;border-top:1px solid var(--line);padding-top:18px}.presence-head{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:500}.presence-dot{width:6px;height:6px;border-radius:50%;background:var(--green)}.presence .meta{margin-top:5px}.presence-steps{display:flex;gap:18px;flex-wrap:wrap;margin-top:15px;color:var(--muted);font-size:11px}.presence-steps span+span:before{content:"→";margin-right:18px;color:#9ba592}.footer{border-top:1px solid var(--line);margin-top:38px;padding-top:17px;display:flex;justify-content:space-between;gap:12px;font-size:10px;letter-spacing:.4px;color:var(--muted)}.readable{display:grid;gap:13px}.readable>div{min-width:0}.readable dt{font-size:11px;color:var(--muted);margin-bottom:3px}.readable dd{margin:0}.readable .readable{border-left:1px solid var(--line);padding-left:13px}.content-list{display:grid;gap:6px;list-style:disc;padding-left:18px}.technical,.sources{font-size:12px;margin-top:12px}.technical summary,.sources summary,.edit-details summary,.review-detail summary{cursor:pointer;color:var(--muted);padding:5px 0;font-size:12px;overflow-wrap:anywhere}.technical[open] summary,.sources[open]>summary{color:var(--green)}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f2ec;border:1px solid var(--line);border-radius:5px;padding:12px;font:11px/1.7 var(--mono);color:#48554a;margin:9px 0}.source-event{border-left:2px solid #d7dfcf;padding:8px 14px;margin:12px 0}.source-event .prose{font-size:13px}.source-heading{font-size:11px;color:var(--muted);margin-bottom:8px}.review-heading{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:9px}.review-heading h3{margin:0}.inbox-section{margin:26px 0}.inbox-section>h2{font-size:14px;margin-bottom:12px}.review-card{padding:24px;border-top:3px solid #c59664}.candidate-content{margin:15px 0}.effect{padding:12px 14px;background:var(--soft);border-radius:5px;font-size:12px;color:#435a45;margin:13px 0}.conflict-note{padding:10px 13px;border-left:2px solid #af6830;background:var(--orange-bg);color:#7b4728;font-size:12px;margin:13px 0}.comparison{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:16px 0}.comparison>div{padding:14px;background:#f3f2ec;border-radius:6px;min-width:0}.comparison>div:last-child{background:#edf2e7}.comparison .caption{display:block;margin-bottom:8px}.comparison .prose{font-size:13px}.review-form{border-top:1px solid var(--line);padding-top:17px;margin-top:18px}.review-fields{display:grid;grid-template-columns:1fr 2fr;gap:16px}label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}input,textarea{width:100%;border:1px solid #c6cebf;border-radius:5px;background:#fffefa;color:var(--ink);padding:9px 11px;font-size:13px}textarea{resize:vertical;min-height:65px}textarea[name=edited_content]{font:12px/1.6 var(--mono)}.review-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:15px}.review-feedback{font-size:12px;margin-top:10px;color:var(--green);overflow-wrap:anywhere}.review-feedback.error{color:#9d3a2f}.review-notice{font-size:13px;padding:13px 16px;margin:18px 0;background:#e8efdf;border:1px solid #b7caa6;border-radius:5px}.review-notice:empty{display:none}.review-mode{font-size:12px;color:var(--muted);line-height:1.9;margin:18px 0}.review-mode code{overflow-wrap:anywhere}.edit-details{margin-top:15px}.friendly-fields{display:grid;gap:12px;margin:12px 0}.archive-toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:20px 0}.archive-toolbar select{border:1px solid var(--line);background:var(--paper);border-radius:5px;padding:7px;font-size:12px;color:var(--ink)}.activity-item{padding:18px 0 18px 22px;border-left:1px solid #d3d9cb;position:relative;margin-left:4px}.activity-item:before{content:"";position:absolute;width:7px;height:7px;border-radius:50%;background:#a3b09a;left:-4px;top:25px;box-shadow:0 0 0 4px var(--bg)}.activity-heading{display:flex;gap:12px;align-items:center;justify-content:space-between;margin-bottom:6px}.activity-heading h3{font-size:13px;font-weight:500}.activity-heading time{font-size:10px;color:var(--muted);white-space:nowrap}.activity-item>.prose,.activity-item .readable{font-size:13px;color:#52604f}.activity-item .technical{margin-top:6px}.archive-note{font-size:12px;color:var(--muted);margin-bottom:20px}.history-group{margin:16px 0}.history-group>summary{font-size:14px;color:var(--green);cursor:pointer;padding:14px 0;border-bottom:1px solid var(--line)}.diff-list,.causal-list{margin:14px 0}.diff-group,.causal-node{border-bottom:1px solid var(--line);padding:12px 0}.diff-group summary,.causal-node summary{cursor:pointer;font-size:13px}.versions,.deps{padding:14px 0 10px 18px;display:grid;gap:12px}.version,.dep{border-left:2px solid #c1ceb5;padding-left:12px;font-size:12px;overflow-wrap:anywhere}.version.retired{color:var(--muted)}.vnum,.dep-role{color:var(--green);margin-right:9px}.vstatus,.delta,.dep-detail{font-size:11px;color:var(--muted);margin-left:6px}.payload{white-space:pre-wrap;overflow-wrap:anywhere}.taste-note{font-size:12px;padding:13px 16px;border-left:2px solid #a88ab9;background:#f2edf4;color:var(--purple);margin-bottom:20px}.browse-feedback{font-size:12px;color:var(--muted);margin-bottom:14px}.filter-empty{padding:24px;border:1px dashed var(--line);border-radius:6px;color:var(--muted);font-size:13px;margin-bottom:20px}.section-note{font-size:12px;color:var(--muted);margin:8px 0 20px}.trunc{color:var(--orange)}.noscript{font-size:12px;color:var(--muted);border:1px solid var(--line);padding:12px;margin-bottom:16px}
@media(min-width:1450px){.main{padding-left:70px;padding-right:70px}}@media(max-width:1050px){.main{padding:26px 30px 48px}.sidebar{padding:30px 18px}.shell{grid-template-columns:200px minmax(0,1fr)}.current-grid{gap:16px}.hero h1{font-size:31px}.card{padding:17px}.comparison,.review-fields{grid-template-columns:1fr}}@media(max-width:740px){.shell{display:block}.sidebar{position:static;height:auto;padding:20px;border-right:0;border-bottom:1px solid var(--line);gap:17px}.sidebar-project,.sidebar-foot,.brand-note{display:none}.wordmark{font-size:24px}.nav{display:flex;gap:5px;overflow-x:auto;padding:3px 0 5px}.nav a{white-space:nowrap;padding:7px 9px;font-size:12px}.nav-number{display:none}.nav-count{margin-left:0}.main{padding:19px 20px 40px}.topbar{flex-wrap:wrap;margin-bottom:24px;padding-bottom:17px;gap:12px}.breadcrumb{font-size:11px}.searchbar{order:3;width:100%}.searchbar input{max-width:none;flex:1}.hero h1{font-size:29px;margin:8px 0 11px}.hero{margin-bottom:25px}.current-grid{grid-template-columns:1fr;gap:15px}.quiet-callout{padding:16px;gap:14px;align-items:flex-start}.quiet-callout .button{padding:7px 10px}.card{padding:18px}.comparison{grid-template-columns:1fr}.review-card{padding:18px}.review-fields{grid-template-columns:1fr}.activity-heading{align-items:flex-start;flex-direction:column;gap:3px}.footer{flex-direction:column;gap:3px}.presence-steps{gap:9px}.presence-steps span+span:before{margin-right:9px}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}@media print{.shell{display:block}.sidebar,.searchbar,.review-form,.footer{display:none}.main{max-width:none;padding:0}.panel[hidden]{display:block!important}.card{break-inside:avoid}body{background:white;color:black}}
'''

BROWSE_SCRIPT = r'''
(() => {
  'use strict';
  const panels = Array.from(document.querySelectorAll('[data-panel]'));
  const links = Array.from(document.querySelectorAll('[data-nav]'));
  const search = document.getElementById('ledger-search');
  const filter = document.getElementById('activity-filter');
  const feedback = document.getElementById('browse-feedback');
  const empty = document.getElementById('filter-empty');
  for (const entry of document.querySelectorAll('#history .diff-group, #history .causal-node')) {
    entry.dataset.search = entry.textContent;
  }
  let current = 'now';
  function applyFilters() {
    const query = search.value.trim().toLocaleLowerCase();
    const panel = panels.find(item => item.id === current);
    const cards = Array.from(panel.querySelectorAll('[data-search]'));
    let matched = 0;
    for (const card of cards) {
      const textMatch = !query || card.dataset.search.toLocaleLowerCase().includes(query);
      const typeMatch = current !== 'archive' || !filter || filter.value === 'all' ||
        card.dataset.signal === filter.value;
      card.hidden = !(textMatch && typeMatch);
      if (query && !card.hidden && current === 'history') {
        const details = card.querySelector(':scope > details');
        if (details) details.open = true;
        const group = card.closest('.history-group');
        if (group) group.open = true;
      }
      if (!card.hidden) matched += 1;
    }
    feedback.hidden = !query;
    feedback.textContent = query ? '当前页面找到 ' + matched + ' 项；可切换左侧页面继续查找。' : '';
    empty.hidden = !((query || cards.length > 0) && matched === 0);
  }
  function showPanel() {
    const requested = location.hash.slice(1);
    current = panels.some(panel => panel.id === requested) ? requested : 'now';
    for (const panel of panels) panel.hidden = panel.id !== current;
    for (const link of links) {
      if (link.hash === '#' + current) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    const title = document.querySelector('#' + current + ' h1');
    document.title = (title ? title.textContent : 'NoName') + ' · NoName 账本';
    applyFilters();
  }
  document.querySelector('.searchbar').classList.add('ready');
  for (const time of document.querySelectorAll('time[datetime]')) {
    const date = new Date(time.dateTime);
    if (!Number.isNaN(date.getTime())) {
      time.title = time.dateTime;
      time.textContent = new Intl.DateTimeFormat('zh-CN', {
        month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', timeZoneName:'short',
      }).format(date);
    }
  }
  search.addEventListener('input', applyFilters);
  document.getElementById('clear-search').addEventListener('click', () => {
    search.value = ''; applyFilters(); search.focus();
  });
  if (filter) filter.addEventListener('change', applyFilters);
  window.addEventListener('hashchange', showPanel);
  for (const link of document.querySelectorAll('a[href^="#"]')) {
    link.addEventListener('click', () => {
      if (panels.some(panel => '#' + panel.id === link.hash)) {
        requestAnimationFrame(() => {
          const heading = document.querySelector(link.hash + ' h1');
          if (heading) heading.focus({preventScroll:true});
          window.scrollTo({top:0,behavior:'instant'});
        });
      }
    });
  }
  showPanel();
})();
'''

# Final spacing rules keep compact controls intact at intermediate widths.
STYLE += r'''
.wordmark{white-space:nowrap}.brand-note{margin-left:0;white-space:nowrap;letter-spacing:1.3px;font-size:9px}.searchbar{flex:0 1 270px;min-width:0}.searchbar input{min-width:0;width:100%;flex:1}.searchbar button{white-space:nowrap;flex:none}.activity-content>summary{cursor:pointer;font-size:12px;color:var(--muted)}.activity-content[open]{margin:10px 0}.panel{scroll-margin-top:22px}@media(max-width:740px){.searchbar{flex:1 0 100%}}
'''
STYLE += r'''
.live-updates{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 16px;margin-bottom:20px;border:1px solid #b7caa6;background:#edf2e7;border-radius:6px;font-size:12px}.live-updates button{flex:none}@media(max-width:740px){.live-updates{align-items:flex-start;flex-direction:column}}
'''
STYLE += r'''
.draft-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
'''
