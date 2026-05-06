// ============================================
// 섹터 페이지 - 동적 렌더링
// URL의 파일명 (e.g. shipbuilding.html) → slug 추출
// ============================================

(function() {
  'use strict';

  // URL에서 slug 추출
  const path = window.location.pathname;
  const m = path.match(/\/sectors\/([^/]+)\.html$/);
  const SLUG = m ? m[1] : null;

  const contentEl = document.getElementById('content');
  const loadingEl = document.getElementById('loading');
  const tradingDateEl = document.getElementById('trading-date');

  // ─────────────────────────────────────────────
  // 유틸
  // ─────────────────────────────────────────────
  const $h = (s) => {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };
  const fmtDate = (iso) => iso ? iso.slice(0, 10).replace(/-/g, '.') : '―';
  const fmtTime = (iso) => iso ? iso.slice(11, 16) : '';
  const fmtNumber = (n) => n == null ? '-' : new Intl.NumberFormat('ko-KR').format(n);
  const fmtChange = (pct) => {
    if (pct == null) return '';
    const cls = pct >= 0 ? 'change-up' : 'change-down';
    const sign = pct >= 0 ? '+' : '';
    return `<span class="change ${cls}">${sign}${pct.toFixed(2)}%</span>`;
  };
  const fmtPriceUSD = (p) => p == null ? '-' : `$${p.toFixed(2)}`;
  const fmtPriceKRW = (p) => p == null ? '-' : new Intl.NumberFormat('ko-KR').format(p);

  // 거래대금 (원 → 억원 변환, 색상)
  function fmtFlow(val) {
    if (val == null || isNaN(val)) return '<span style="color:#aaa;">-</span>';
    const v = Number(val);
    const eok = v / 100_000_000;
    const sign = v > 0 ? '+' : '';
    const cls = v > 0 ? 'change-up' : (v < 0 ? 'change-down' : '');
    const txt = `${sign}${eok.toFixed(0)}`;
    return `<span class="${cls}">${txt}</span>`;
  }
  function flowTable(flows) {
    if (!flows || flows.length === 0) return '';
    const rows = flows.map(f => `
      <tr>
        <td class="flow-date">${fmtDate(f.date)}</td>
        <td>${fmtFlow(f['외인'])}</td>
        <td>${fmtFlow(f['기관'])}</td>
        <td>${fmtFlow(f['개인'])}</td>
      </tr>
    `).join('');
    return `
      <table class="flow-table">
        <thead>
          <tr>
            <th>일자</th>
            <th>외인</th>
            <th>기관</th>
            <th>개인</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  // ─────────────────────────────────────────────
  // AI 요약 박스
  // ─────────────────────────────────────────────
  function renderSummary(data) {
    const bullets = data.ai_summary || [];
    if (bullets.length === 0) return '';
    return `
      <section class="summary-box">
        <div class="summary-label">
          <span class="summary-tag">AI</span>
          <span class="summary-title">오늘의 섹터 요약</span>
          <span class="summary-time">${fmtTime(data.generated_at)} 생성</span>
        </div>
        <ul class="summary-list">
          ${bullets.map(b => `<li>${$h(b)}</li>`).join('')}
        </ul>
      </section>
    `;
  }

  // ─────────────────────────────────────────────
  // 섹터 핵심 이슈 (뉴스)
  // ─────────────────────────────────────────────
  function sentimentTag(s) {
    if (s === 'positive') return '<span class="tag tag-positive">긍정</span>';
    if (s === 'negative') return '<span class="tag tag-negative">부정</span>';
    return '<span class="tag tag-neutral">중립</span>';
  }
  function renderNews(data) {
    const news = data.news || [];
    if (news.length === 0) {
      return `
        <section class="block">
          <div class="block-header">
            <h2 class="block-title">섹터 핵심 이슈</h2>
            <span class="block-count">0건</span>
          </div>
          <div class="empty-state">오늘 보고할 핵심 뉴스가 없습니다.</div>
        </section>
      `;
    }
    const total = news.length;
    const items = news.map((n, idx) => `
      <li class="news-item" data-idx="${idx}">
        <div class="news-meta">
          ${sentimentTag(n.sentiment)}
          ${n.is_new ? '<span class="new-dot" title="신규"></span>' : ''}
          <span class="news-date">${fmtDate(n.published_at)} ${fmtTime(n.published_at)}</span>
          <span class="news-source">${$h(n.source)}</span>
        </div>
        <a href="${$h(n.url)}" target="_blank" rel="noopener" class="news-title">${$h(n.title)}</a>
        ${n.summary ? `<p class="news-summary">${$h(n.summary)}</p>` : ''}
      </li>
    `).join('');
    return `
      <section class="block" id="news-block">
        <div class="block-header">
          <h2 class="block-title">섹터 핵심 이슈</h2>
          <span class="block-count" id="news-count">3 / ${total}건</span>
        </div>
        <ul class="news-list" id="news-list">${items}</ul>
        ${total > 3 ? `
          <button class="show-more-btn" id="show-more-news">
            <span class="show-more-text">더보기 (${Math.min(10, total) - 3}건 더)</span>
            <span class="show-less-text">접기</span>
          </button>
        ` : ''}
      </section>
    `;
  }

  // ─────────────────────────────────────────────
  // 미국 시장 동향 (관련) - 카드 클릭 시 기업개요 펼침, 디폴트 접기, 더보기 3건
  // ─────────────────────────────────────────────
  function renderUSMovers(data) {
    const movers = data.us_movers || [];
    if (movers.length === 0) return '';
    const total = movers.length;

    const items = movers.map((m, idx) => {
      const badges = [];
      if (m.is_52w_high) badges.push('<span class="badge-high">52주 최고</span>');
      if (m.is_52w_low) badges.push('<span class="badge-low">52주 최저</span>');
      const hasDesc = (m.description && m.description.trim());
      return `
        <details class="us-mover-card" data-idx="${idx}">
          <summary class="us-mover-summary">
            <span class="ticker">${$h(m.ticker)}-US</span>
            <span class="us-name">${$h(m.name)}</span>
            <span class="us-price">${fmtPriceUSD(m.price)}</span>
            ${fmtChange(m.change_pct)}
            ${badges.join('')}
            ${hasDesc ? '<span class="us-mover-toggle">▾</span>' : ''}
          </summary>
          ${hasDesc ? `
            <div class="us-mover-detail">
              <div class="us-mover-desc-label">기업개요</div>
              <div class="us-mover-desc">${$h(m.description)}</div>
            </div>
          ` : ''}
        </details>
      `;
    }).join('');

    return `
      <section class="block" id="us-movers-block">
        <div class="block-header">
          <h2 class="block-title">미국 시장 동향 (관련)</h2>
          <span class="block-count" id="us-movers-count">3 / ${total}건</span>
        </div>
        <div class="us-movers-list" id="us-movers-list">${items}</div>
        ${total > 3 ? `
          <button class="show-more-btn" id="show-more-us">
            <span class="show-more-text">더보기 (${total - 3}건 더)</span>
            <span class="show-less-text">접기</span>
          </button>
        ` : ''}
      </section>
    `;
  }

  // ─────────────────────────────────────────────
  // 종목별 상세
  // ─────────────────────────────────────────────
  function disclosureItems(arr) {
    if (!arr || arr.length === 0) return '<li class="empty-line">데이터 없음</li>';
    return arr.map(d => `
      <li>
        ${d.is_new ? '<span class="new-dot"></span>' : ''}
        <span class="line-date">${fmtDate(d.date)}</span>
        ${d.url ? `<a href="${$h(d.url)}" target="_blank" rel="noopener">${$h(d.title)}</a>` : `<span>${$h(d.title)}</span>`}
      </li>
    `).join('');
  }
  function reportItems(arr) {
    if (!arr || arr.length === 0) return '<li class="empty-line">데이터 없음</li>';
    return arr.map(r => {
      const hasDetail = (r.summary && r.summary.trim()) || (r.key_points && r.key_points.length);
      const keyPointsHtml = (r.key_points && r.key_points.length)
        ? `<ul class="report-keypoints">${r.key_points.map(k => `<li>${$h(k)}</li>`).join('')}</ul>`
        : '';
      const summaryHtml = r.summary
        ? `<div class="report-summary">${$h(r.summary)}</div>`
        : '';
      const detailBlock = hasDetail
        ? `<div class="report-detail">${keyPointsHtml}${summaryHtml}</div>`
        : '';

      return `
        <li class="report-item">
          <div class="report-head">
            ${r.is_new ? '<span class="new-dot"></span>' : ''}
            <span class="line-date">${fmtDate(r.date)}</span>
            <span class="line-broker">${$h(r.broker)}</span>
            ${r.url ? `<a href="${$h(r.url)}" target="_blank" rel="noopener" class="report-title">${$h(r.title)}</a>` : `<span class="report-title">${$h(r.title)}</span>`}
            ${hasDetail ? '<button class="report-toggle" type="button" aria-label="펼치기">▾</button>' : ''}
          </div>
          ${detailBlock}
        </li>
      `;
    }).join('');
  }

  // 종목별 뉴스 (stock.news) — 펼쳐진 종목 카드 안에서 표시
  function stockNewsItems(arr) {
    if (!arr || arr.length === 0) return '<li class="empty-line">데이터 없음</li>';
    return arr.map(n => `
      <li class="stock-news-item">
        <div class="stock-news-meta">
          ${sentimentTag(n.sentiment)}
          <span class="line-date">${fmtDate(n.published_at)} ${fmtTime(n.published_at)}</span>
        </div>
        ${n.url ? `<a href="${$h(n.url)}" target="_blank" rel="noopener" class="stock-news-title">${$h(n.title)}</a>` : `<span class="stock-news-title">${$h(n.title)}</span>`}
        ${n.summary ? `<p class="stock-news-summary">${$h(n.summary)}</p>` : ''}
      </li>
    `).join('');
  }
  function consensusOneSide(arr, term) {
    if (!arr || arr.length === 0) {
      return `<div class="cons-side cons-empty">${term === 'Y' ? '연간' : '분기'} 컨센서스 변화 없음</div>`;
    }
    const items = arr.map(c => {
      const arrow = c.previous != null && c.value != null
        ? `<span class="cons-arrow">${fmtNumber(c.previous)} → ${fmtNumber(c.value)}</span>`
        : c.value != null ? `<span class="cons-arrow">${fmtNumber(c.value)}</span>` : '';
      const pct = c.change_pct != null
        ? `<span class="cons-pct ${c.change_pct >= 0 ? 'change-up' : 'change-down'}">${c.change_pct >= 0 ? '+' : ''}${c.change_pct.toFixed(1)}%</span>`
        : '';
      return `
        <li>
          ${c.is_new ? '<span class="new-dot"></span>' : ''}
          <span class="line-date">${fmtDate(c.date)}</span>
          ${c.period ? `<span class="cons-period">${$h(c.period)}</span>` : ''}
          ${arrow} ${pct}
        </li>
      `;
    }).join('');
    return `
      <div class="cons-side">
        <div class="cons-side-label">${term === 'Y' ? '연간' : '분기'}</div>
        <ul class="cons-list">${items}</ul>
      </div>
    `;
  }
  function consensusBlock(arrQ, arrY) {
    return `
      <div class="cons-grid">
        ${consensusOneSide(arrQ, 'Q')}
        ${consensusOneSide(arrY, 'Y')}
      </div>
    `;
  }

  function renderStock(s, idx) {
    const priceHtml = s.has_chart && s.price != null
      ? `<div class="stock-price">
           <span class="price-num">${fmtPriceKRW(s.price)}</span>
           ${fmtChange(s.change_pct)}
         </div>`
      : `<div class="stock-price"><span class="price-num">-</span></div>`;

    const counts = [
      `뉴스 ${(s.news || []).length}`,
      `공시 ${(s.disclosures || []).length}`,
      `컨센 ${((s.consensus?.Q || []).length + (s.consensus?.Y || []).length)}`,
      `리포트 ${(s.reports || []).length}`,
    ];

    const dataName = `${s.name} ${s.code}`.toLowerCase();

    return `
      <details class="stock-card" data-name="${$h(dataName)}" data-code="${$h(s.code)}" data-disclosure="${(s.disclosures || []).length}">
        <summary class="stock-summary">
          <div class="stock-head">
            <span class="stock-code">${$h(s.code)}</span>
            <span class="stock-name">${$h(s.name)}</span>
          </div>
          ${priceHtml}
          <div class="stock-counts">
            ${counts.map(c => `<span class="badge">${$h(c)}</span>`).join('')}
          </div>
        </summary>
        <div class="stock-detail">
          ${s.has_chart ? `
            <div class="detail-row">
              <h4 class="detail-label">📈 가격 차트</h4>
              <div class="chart-toolbar">
                <div class="chart-tabs">
                  <button class="chart-tab active" data-range="1M">1M</button>
                  <button class="chart-tab" data-range="3M">3M</button>
                  <button class="chart-tab" data-range="1Y">1Y</button>
                  <button class="chart-tab" data-range="YTD">YTD</button>
                </div>
                <div class="chart-range-change"></div>
              </div>
              <div class="chart-wrap">
                <canvas class="stock-chart"></canvas>
                <div class="chart-loading">차트 로딩 중...</div>
              </div>
            </div>
          ` : ''}
          ${(s.stk_flow && s.stk_flow.length > 0) ? `
            <div class="detail-row">
              <h4 class="detail-label">💰 거래대금 (최근 ${s.stk_flow.length}거래일, 단위: 억원)</h4>
              ${flowTable(s.stk_flow)}
            </div>
          ` : ''}
          ${(s.news && s.news.length > 0) ? `
            <div class="detail-row">
              <h4 class="detail-label">📰 종목 뉴스 (어제자, 핵심 ${s.news.length}건)</h4>
              <ul class="stock-news-list">${stockNewsItems(s.news)}</ul>
            </div>
          ` : ''}
          <div class="detail-row">
            <h4 class="detail-label">📄 공시 (7일)</h4>
            <ul class="detail-list">${disclosureItems(s.disclosures)}</ul>
          </div>
          <div class="detail-row">
            <h4 class="detail-label">💹 영업이익 컨센서스 변화 (최근 5건)</h4>
            ${consensusBlock(s.consensus?.Q, s.consensus?.Y)}
          </div>
          <div class="detail-row">
            <h4 class="detail-label">📑 타사 리포트 (7일)</h4>
            <ul class="detail-list">${reportItems(s.reports)}</ul>
          </div>
        </div>
      </details>
    `;
  }

  function renderStocks(data) {
    const stocks = data.stocks || [];
    if (stocks.length === 0) {
      return `
        <section class="block">
          <div class="block-header">
            <h2 class="block-title">종목별 상세</h2>
            <span class="block-count">0종목</span>
          </div>
          <div class="empty-state">커버 종목이 없습니다.</div>
        </section>
      `;
    }
    return `
      <section class="block" id="stocks-block">
        <div class="block-header">
          <h2 class="block-title">종목별 상세</h2>
          <div class="block-controls">
            <input type="text" class="block-search" id="stock-search" placeholder="종목 검색...">
            <select class="block-sort" id="stock-sort">
              <option value="default">변동률순</option>
              <option value="name">종목명순</option>
              <option value="disclosure">공시 많은순</option>
            </select>
          </div>
        </div>
        <div class="stock-list" id="stock-list">
          ${stocks.map((s, idx) => renderStock(s, idx)).join('')}
        </div>
      </section>
    `;
  }

  // ─────────────────────────────────────────────
  // 메인 렌더
  // ─────────────────────────────────────────────
  function renderAll(data) {
    tradingDateEl.textContent = fmtDate(data.trading_date);
    document.title = `${data.wics_3rd} - 섹터별 이슈 브리핑`;

    const breadcrumb = `
      <nav class="breadcrumb">
        <a href="../index.html">전체</a><span class="bc-sep">›</span>
        <span>${$h(data.wics_1st)}</span><span class="bc-sep">›</span>
        <span>${$h(data.wics_2nd)}</span><span class="bc-sep">›</span>
        <span class="bc-current">${$h(data.wics_3rd)}</span>
      </nav>
      <h1 class="sector-title">${$h(data.wics_3rd)}</h1>
    `;

    contentEl.innerHTML = breadcrumb
      + renderSummary(data)
      + renderNews(data)
      + renderUSMovers(data)
      + renderStocks(data);

    bindNewsToggle(data);
    bindUSMoversToggle(data);
    bindStockSearchSort();
    bindChartLazy();
    bindReportToggles();
  }

  // ─────────────────────────────────────────────
  // US movers 더보기 토글 (디폴트 3건)
  // ─────────────────────────────────────────────
  function bindUSMoversToggle(data) {
    const list = document.getElementById('us-movers-list');
    const btn  = document.getElementById('show-more-us');
    const cnt  = document.getElementById('us-movers-count');
    if (!list) return;
    const items = Array.from(list.querySelectorAll('.us-mover-card'));
    const total = items.length;
    const DEFAULT = 3;
    let expanded = false;
    function apply() {
      const target = expanded ? total : DEFAULT;
      items.forEach((it, i) => { it.style.display = i < target ? '' : 'none'; });
      if (cnt) cnt.textContent = `${target} / ${total}건`;
      if (btn) btn.classList.toggle('expanded', expanded);
    }
    apply();
    if (btn) btn.addEventListener('click', () => { expanded = !expanded; apply(); });
  }

  // ─────────────────────────────────────────────
  // 리포트 펼치기/접기
  // ─────────────────────────────────────────────
  function bindReportToggles() {
    document.querySelectorAll('.report-item').forEach(li => {
      const btn = li.querySelector('.report-toggle');
      if (!btn) return;
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        const expanded = li.classList.toggle('expanded');
        btn.textContent = expanded ? '▴' : '▾';
      });
    });
  }

  // ─────────────────────────────────────────────
  // 더보기 토글 (뉴스)
  // ─────────────────────────────────────────────
  function bindNewsToggle(data) {
    const newsList = document.getElementById('news-list');
    const btn = document.getElementById('show-more-news');
    const cnt = document.getElementById('news-count');
    if (!newsList || !btn) return;
    const items = Array.from(newsList.querySelectorAll('.news-item'));
    const total = items.length;
    const DEFAULT = 3, MAX = 10;
    let expanded = false;
    function apply() {
      const target = expanded ? Math.min(MAX, total) : DEFAULT;
      items.forEach((it, i) => { it.style.display = i < target ? '' : 'none'; });
      if (cnt) cnt.textContent = `${target} / ${total}건`;
      btn.classList.toggle('expanded', expanded);
    }
    apply();
    btn.addEventListener('click', () => { expanded = !expanded; apply(); });
  }

  // ─────────────────────────────────────────────
  // 종목 검색/정렬
  // ─────────────────────────────────────────────
  function bindStockSearchSort() {
    const list = document.getElementById('stock-list');
    if (!list) return;
    const search = document.getElementById('stock-search');
    const sort = document.getElementById('stock-sort');
    const cards = Array.from(list.querySelectorAll('.stock-card'));

    search.addEventListener('input', (e) => {
      const q = e.target.value.trim().toLowerCase();
      cards.forEach(c => {
        c.style.display = c.dataset.name.includes(q) ? '' : 'none';
      });
    });

    sort.addEventListener('change', (e) => {
      const v = e.target.value;
      const arr = [...cards];
      if (v === 'name') {
        arr.sort((a, b) => a.dataset.name.localeCompare(b.dataset.name, 'ko'));
      } else if (v === 'disclosure') {
        arr.sort((a, b) => parseInt(b.dataset.disclosure) - parseInt(a.dataset.disclosure));
      } else {
        // default - 원래 순서 (변동률순, 서버에서 이미 정렬됨)
        return;
      }
      arr.forEach(c => list.appendChild(c));
    });
  }

  // ─────────────────────────────────────────────
  // 차트 lazy load
  // ─────────────────────────────────────────────
  const chartCache = {};   // code → data array
  const chartInstances = {};  // code → Chart instance

  async function loadChartData(code) {
    if (chartCache[code]) return chartCache[code];
    const resp = await fetch(`../data/charts/${code}.json?_=${Date.now()}`);
    if (!resp.ok) throw new Error(`chart fetch failed: ${resp.status}`);
    const data = await resp.json();
    chartCache[code] = data.data || [];
    return chartCache[code];
  }

  function sliceByRange(data, range) {
    if (!data || data.length === 0) return [];
    const last = data[data.length - 1].date;
    const lastDate = new Date(last);
    let from = new Date(lastDate);
    if (range === '1M') from.setMonth(from.getMonth() - 1);
    else if (range === '3M') from.setMonth(from.getMonth() - 3);
    else if (range === '1Y') from.setFullYear(from.getFullYear() - 1);
    else if (range === 'YTD') from = new Date(lastDate.getFullYear(), 0, 1);
    else return data;
    const fromStr = from.toISOString().slice(0, 10);
    return data.filter(d => d.date >= fromStr);
  }

  function calcRangeChange(sliced) {
    if (!sliced || sliced.length < 2) return null;
    const first = sliced[0].close;
    const last = sliced[sliced.length - 1].close;
    if (!first) return null;
    return (last - first) / first * 100;
  }

  function updateRangeChange(card, sliced) {
    const el = card.querySelector('.chart-range-change');
    if (!el) return;
    const pct = calcRangeChange(sliced);
    if (pct == null) {
      el.innerHTML = '';
      return;
    }
    const cls = pct >= 0 ? 'change-up' : 'change-down';
    const sign = pct >= 0 ? '+' : '';
    el.innerHTML = `<span class="${cls}">${sign}${pct.toFixed(2)}%</span>`;
  }

  function drawChart(canvas, data, range) {
    const card = canvas.closest('.stock-card');
    const code = card.dataset.code;
    const sliced = sliceByRange(data, range);
    updateRangeChange(card, sliced);
    if (chartInstances[code]) {
      chartInstances[code].destroy();
    }
    const ctx = canvas.getContext('2d');
    chartInstances[code] = new Chart(ctx, {
      type: 'line',
      data: {
        labels: sliced.map(d => d.date),
        datasets: [{
          data: sliced.map(d => d.close),
          borderColor: '#111',
          borderWidth: 1.5,
          fill: false,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => items[0].label,
              label: (item) => '종가: ' + new Intl.NumberFormat('ko-KR').format(item.parsed.y),
            }
          }
        },
        scales: {
          x: {
            ticks: { maxTicksLimit: 5, font: { size: 10 } },
            grid: { display: false },
          },
          y: {
            ticks: {
              font: { size: 10 },
              callback: (v) => new Intl.NumberFormat('ko-KR').format(v),
            },
            grid: { color: '#f0f0f0' },
          },
        },
      },
    });
  }

  function bindChartLazy() {
    document.querySelectorAll('.stock-card').forEach(card => {
      const canvas = card.querySelector('.stock-chart');
      if (!canvas) return;
      const code = card.dataset.code;
      const tabs = card.querySelectorAll('.chart-tab');
      const wrap = card.querySelector('.chart-wrap');
      const loadingDiv = card.querySelector('.chart-loading');
      let loaded = false;
      let currentRange = '1M';

      // 펼칠 때 차트 로드
      card.addEventListener('toggle', async () => {
        if (!card.open || loaded) return;
        try {
          const data = await loadChartData(code);
          if (loadingDiv) loadingDiv.style.display = 'none';
          drawChart(canvas, data, currentRange);
          loaded = true;
        } catch (e) {
          if (loadingDiv) loadingDiv.textContent = '차트 데이터 없음';
        }
      });

      // 탭 전환
      tabs.forEach(tab => {
        tab.addEventListener('click', async () => {
          tabs.forEach(t => t.classList.remove('active'));
          tab.classList.add('active');
          currentRange = tab.dataset.range;
          if (!loaded) return;
          const data = chartCache[code];
          if (data) drawChart(canvas, data, currentRange);
        });
      });
    });
  }

  // ─────────────────────────────────────────────
  // 진입
  // ─────────────────────────────────────────────
  async function load() {
    if (!SLUG) {
      loadingEl.textContent = 'URL이 올바르지 않습니다.';
      return;
    }
    try {
      const resp = await fetch(`../data/sectors/${SLUG}.json?_=${Date.now()}`);
      if (!resp.ok) throw new Error(`섹터 데이터 fetch 실패: ${resp.status}`);
      const data = await resp.json();
      renderAll(data);
    } catch (e) {
      console.error(e);
      contentEl.innerHTML = `
        <div class="loading-state error">
          데이터를 불러올 수 없습니다.<br>
          <small>slug: ${$h(SLUG)} / ${$h(String(e))}</small>
        </div>
      `;
    }
  }

  load();
})();
