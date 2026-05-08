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

  // 거래대금/컨센서스 포맷: 원 → "1조 2,345억원" / 1억 미만 "0.12억원"
  function fmtKRW(val) {
    if (val == null || isNaN(val)) return '-';
    const v = Number(val);
    const neg = v < 0;
    const abs_v = Math.abs(v);
    let txt;
    if (abs_v >= 1_000_000_000_000) {
      const jo = Math.floor(abs_v / 1_000_000_000_000);
      const eok = Math.round((abs_v % 1_000_000_000_000) / 100_000_000);
      txt = eok > 0 ? `${jo.toLocaleString()}조 ${eok.toLocaleString()}억` : `${jo.toLocaleString()}조`;
    } else if (abs_v >= 100_000_000) {
      txt = `${Math.round(abs_v / 100_000_000).toLocaleString()}억`;
    } else {
      txt = `${(abs_v / 100_000_000).toFixed(2)}억`;
    }
    return neg ? `-${txt}` : txt;
  }

  // 거래대금 (원 → "1조 2,345억" / "0.12억" + 색상, 셀 내 '원' 표기 없음)
  function fmtFlow(val) {
    if (val == null || isNaN(val)) return '<span style="color:#aaa;">-</span>';
    const v = Number(val);
    const sign = v > 0 ? '+' : '';
    const cls = v > 0 ? 'change-up' : (v < 0 ? 'change-down' : '');
    return `<span class="${cls}">${sign}${fmtKRW(v)}</span>`;
  }
  function flowTable(flows) {
    if (!flows || flows.length === 0) return '<div class="empty-state">거래대금 데이터 없음</div>';
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
        <thead><tr><th>일자</th><th>외인</th><th>기관</th><th>개인</th></tr></thead>
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
    const items = news.map((n, idx) => {
      const kwTag = n.keyword ? `<span class="news-kw-tag">${$h(n.keyword)}</span>` : '';
      const summaryBox = n.summary
        ? `<details class="news-summary-toggle"><summary class="news-summary-btn">요약 보기</summary><div class="news-summary-box">${$h(n.summary)}</div></details>`
        : '';
      return `
      <li class="news-item" data-idx="${idx}">
        <div class="news-meta">
          ${sentimentTag(n.sentiment)}
          ${kwTag}
          ${n.is_new ? '<span class="new-dot" title="신규"></span>' : ''}
          <span class="news-date">${fmtDate(n.published_at)} ${fmtTime(n.published_at)}</span>
          <span class="news-source">${$h(n.source)}</span>
        </div>
        <a href="${$h(n.url)}" target="_blank" rel="noopener" class="news-title">${$h(n.title)}</a>
        ${summaryBox}
      </li>
    `}).join('');
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
  // US peer 동향 - 카드 클릭 시 기업개요 펼침, 디폴트 접기, 더보기 3건
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
            <span class="us-mover-toggle">▾</span>
          </summary>
          <div class="us-mover-detail">
            <div class="us-mover-reason">${$h(m.reason)}</div>
            ${(m.news_urls && m.news_urls.length) ? `
              <div class="us-mover-news">
                ${m.news_urls.map(n => `<a href="${$h(n.url)}" target="_blank" rel="noopener" class="us-news-link">📰 ${$h(n.title || '관련 뉴스')}</a>`).join('')}
              </div>
            ` : ''}
            ${hasDesc ? `
              <div class="us-mover-desc-label">기업개요</div>
              <div class="us-mover-desc">${$h(m.description)}</div>
            ` : ''}
            ${(m.earnings && m.earnings.length) ? `
              <div class="us-earnings-label">실적 (EPS)</div>
              <table class="us-earnings-table">
                <thead><tr><th>발표일</th><th>추정</th><th>실적</th><th>서프라이즈</th></tr></thead>
                <tbody>
                  ${m.earnings.map(e => {
                    const surp = e.surprise_pct != null
                      ? `<span class="${e.surprise_pct >= 0 ? 'change-up' : 'change-down'}">${e.surprise_pct >= 0 ? '+' : ''}${e.surprise_pct.toFixed(2)}%</span>`
                      : '-';
                    return `<tr>
                      <td>${e.date || '-'}</td>
                      <td>${e.eps_est != null ? '$'+e.eps_est.toFixed(2) : '-'}</td>
                      <td>${e.eps_actual != null ? '$'+e.eps_actual.toFixed(2) : '-'}</td>
                      <td>${surp}</td>
                    </tr>`;
                  }).join('')}
                </tbody>
              </table>
            ` : ''}
          </div>
        </details>
      `;
    }).join('');

    return `
      <section class="block" id="us-movers-block">
        <div class="block-header">
          <h2 class="block-title">US peer 동향</h2>
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
    if (!arr || arr.length === 0) return '<li class="empty-state">공시 데이터 없음</li>';
    return arr.map(d => `
      <li>
        ${d.is_new ? '<span class="new-dot"></span>' : ''}
        <span class="line-date">${fmtDate(d.date)}</span>
        ${d.url ? `<a href="${$h(d.url)}" target="_blank" rel="noopener">${$h(d.title)}</a>` : `<span>${$h(d.title)}</span>`}
      </li>
    `).join('');
  }
  function reportItems(arr) {
    if (!arr || arr.length === 0) return '<li class="empty-state">리포트 데이터 없음</li>';
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
    if (!arr || arr.length === 0) return '<li class="empty-state">종목 뉴스 없음</li>';
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
  // 컨센서스: dict {period, latest, is_new, series: [{date, value}]} → 라인차트
  function consensusOneSide(c, term) {
    const label = term === 'Y' ? '연간' : '분기';
    if (!c || !c.series || c.series.length === 0) {
      return `<div class="cons-side cons-empty">${label} 데이터 없음</div>`;
    }
    const period = c.period || label;
    const latest = c.latest != null ? `${fmtKRW(c.latest)}` : '-';
    const newDot = c.is_new ? '<span class="new-dot" style="margin-right:4px;"></span>' : '';
    return `
      <div class="cons-side">
        <div class="cons-side-header">
          ${newDot}<span class="cons-period-label">${$h(period)}</span>
          <span class="cons-latest">: ${latest}</span>
        </div>
        <div style="position:relative;height:120px;width:100%;overflow:hidden;">
          <canvas class="cons-chart" data-series='${JSON.stringify(c.series)}'></canvas>
        </div>
      </div>
    `;
  }
  function consensusBlock(cQ, cY) {
    return `
      <div class="cons-grid">
        ${consensusOneSide(cQ, 'Q')}
        ${consensusOneSide(cY, 'Y')}
      </div>
    `;
  }

  function renderStock(s, idx) {
    // 시총 표기: 100조+ → 100조원, 1~100조 → 1.23조원, <1조 → 9,899억원
    let capStr = '';
    if (s.market_cap != null) {
      const v = Number(s.market_cap);
      const abs_v = Math.abs(v);
      if (abs_v >= 100_000_000_000_000) {
        capStr = `${Math.round(abs_v / 1_000_000_000_000)}조원`;
      } else if (abs_v >= 1_000_000_000_000) {
        capStr = `${(abs_v / 1_000_000_000_000).toFixed(2)}조원`;
      } else if (abs_v >= 100_000_000) {
        capStr = `${Math.round(abs_v / 100_000_000).toLocaleString()}억원`;
      } else if (abs_v > 0) {
        capStr = `${(abs_v / 100_000_000).toFixed(2)}억원`;
      }
    }
    const capHtml = capStr ? `시총:${capStr}` : '';
    const peHtml = s.fwd_pe != null ? `fwd P/E:${s.fwd_pe.toFixed(1)}배` : '';
    const metaParts = [capHtml, peHtml].filter(Boolean);
    const metaHtml = metaParts.length
      ? `<div class="stock-meta-row">${metaParts.join(' | ')}</div>`
      : '';
    const newDotHtml = s.has_new
      ? '<span class="card-new-dot" title="당일 신규 이슈"></span>'
      : '';
    const priceHtml = s.has_chart && s.price != null
      ? `<div class="stock-price">
           <span class="price-num">${fmtPriceKRW(s.price)}</span>
           ${fmtChange(s.change_pct)}
         </div>`
      : `<div class="stock-price"><span class="price-num">-</span></div>`;

    // 당일 신규 항목만 표시 (숫자 없이 텍스트만)
    const todayBadges = [];
    if ((s.news || []).some(n => n.is_new)) todayBadges.push('뉴스');
    if ((s.disclosures || []).some(d => d.is_new)) todayBadges.push('공시');
    const cy = s.consensus?.Y || {};
    const cq = s.consensus?.Q || {};
    if ((typeof cy === 'object' && cy.is_new) || (typeof cq === 'object' && cq.is_new)) todayBadges.push('컨센');
    if ((s.reports || []).some(r => r.is_new)) todayBadges.push('리포트');

    const dataName = `${s.name} ${s.code}`.toLowerCase();

    return `
      <details class="stock-card${s.has_new ? ' has-new' : ''}" data-name="${$h(dataName)}" data-code="${$h(s.code)}" data-change="${Math.abs(s.change_pct || 0)}">
        <summary class="stock-summary">
          <div class="stock-head">
            ${newDotHtml}
            <span class="stock-code">${$h(s.code)}</span>
            <span class="stock-name">${$h(s.name)}</span>
          </div>
          ${priceHtml}
          ${metaHtml}
          <div class="stock-counts">
            ${todayBadges.map(c => `<span class="badge">${$h(c)}</span>`).join('')}
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
              <h4 class="detail-label">💰 거래대금 (최근 ${s.stk_flow.length}거래일, 단위: 원)</h4>
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
            <h4 class="detail-label">💹 영업이익 컨센서스 변화 (최근 1개월, 단위: 억원)</h4>
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

    const sectorReturnHtml = data.sector_return != null
      ? `<div class="sector-return-badge">${fmtChange(data.sector_return)}</div>`
      : '';

    const breadcrumb = `
      <nav class="breadcrumb">
        <a href="../index.html">전체</a><span class="bc-sep">›</span>
        <span>${$h(data.wics_1st)}</span><span class="bc-sep">›</span>
        <span>${$h(data.wics_2nd)}</span><span class="bc-sep">›</span>
        <span class="bc-current">${$h(data.wics_3rd)}</span>
      </nav>
      <div class="sector-title-row">
        <h1 class="sector-title">${$h(data.wics_3rd)}</h1>
        ${sectorReturnHtml}
      </div>
    `;

    contentEl.innerHTML = breadcrumb
      + renderSummary(data)
      + renderNews(data)
      + renderGlobalResearch(data)
      + renderUSMovers(data)
      + renderStocks(data);

    bindNewsToggle(data);
    bindUSMoversToggle(data);
    bindStockSearchSort();
    bindChartLazy();
    bindReportToggles();
  }

  // ─────────────────────────────────────────────
  // 독점 글로벌 리서치 (KIS 글로벌 리서치)
  // ─────────────────────────────────────────────
  function renderGlobalResearch(data) {
    const items = data.global_research || [];
    if (items.length === 0) {
      return `
        <section class="block">
          <div class="block-header">
            <h2 class="block-title">📚 독점 글로벌 리서치</h2>
          </div>
          <div class="empty-state">오늘 발행된 리서치 없음</div>
        </section>
      `;
    }
    const itemsHtml = items.slice(0, 10).map(r => {
      const cat = r.category ? `<span class="gr-cat">${$h(r.category)}</span>` : '';
      const summary = r.summary
        ? `<details class="news-summary-toggle"><summary class="news-summary-btn">요약 보기</summary><div class="news-summary-box">${$h(r.summary)}</div></details>`
        : '';
      const titleHtml = r.url
        ? `<a href="${$h(r.url)}" target="_blank" rel="noopener" class="news-title-link">${$h(r.title)}</a>`
        : `<span class="news-title-link">${$h(r.title)}</span>`;
      return `
        <li class="news-item">
          <div class="news-meta">
            ${cat}
            ${r.published_at ? `<span class="news-date">${fmtDate(r.published_at)}</span>` : ''}
            ${r.publisher ? `<span class="news-source">${$h(r.publisher)}</span>` : ''}
          </div>
          ${titleHtml}
          ${summary}
        </li>
      `;
    }).join('');
    return `
      <section class="block">
        <div class="block-header">
          <h2 class="block-title">📚 독점 글로벌 리서치</h2>
          <span class="block-count">${items.length}건</span>
        </div>
        <ul class="news-list">${itemsHtml}</ul>
      </section>
    `;
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

    sort.addEventListener('change', doSort);
    function doSort() {
      const v = sort.value;
      const arr = [...cards];
      if (v === 'name') {
        arr.sort((a, b) => a.dataset.name.localeCompare(b.dataset.name, 'ko'));
      } else if (v === 'disclosure') {
        arr.sort((a, b) => (parseFloat(b.dataset.disclosure) || 0) - (parseFloat(a.dataset.disclosure) || 0));
      } else {
        // 변동률순 (|change_pct| 내림차순)
        arr.sort((a, b) => (parseFloat(b.dataset.change) || 0) - (parseFloat(a.dataset.change) || 0));
      }
      arr.forEach(c => list.appendChild(c));
    }
    // 최초 로드 시도 변동률순 정렬 적용
    doSort();
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
      const code = card.dataset.code;
      const tabs = card.querySelectorAll('.chart-tab');
      const loadingDiv = card.querySelector('.chart-loading');
      let loaded = false;
      let currentRange = '1M';

      card.addEventListener('toggle', async () => {
        if (!card.open) return;
        // 컨센 라인차트 그리기 (1회만)
        card.querySelectorAll('.cons-chart').forEach(cv => {
          if (cv.dataset.drawn === '1') return;
          drawConsensusChart(cv);
          cv.dataset.drawn = '1';
        });
        // 가격 차트
        if (canvas && !loaded) {
          try {
            const data = await loadChartData(code);
            if (loadingDiv) loadingDiv.style.display = 'none';
            drawChart(canvas, data, currentRange);
            loaded = true;
          } catch (e) {
            if (loadingDiv) loadingDiv.textContent = '차트 데이터 없음';
          }
        }
      });

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

  // 컨센서스 라인차트 (Chart.js)
  function drawConsensusChart(canvas) {
    const seriesRaw = canvas.dataset.series;
    if (!seriesRaw) return;
    let series;
    try { series = JSON.parse(seriesRaw); } catch (e) { return; }
    if (!series || series.length === 0) return;

    // canvas 크기 고정 (무한 확장 방지)
    canvas.style.height = '120px';
    canvas.style.maxHeight = '120px';
    canvas.height = 120;
    const parent = canvas.parentElement;
    if (parent) { parent.style.height = '140px'; parent.style.maxHeight = '140px'; }

    const ctx = canvas.getContext('2d');
    new Chart(ctx, {
      type: 'line',
      data: {
        labels: series.map(p => (p.date || '').slice(5)),  // MM-DD만
        datasets: [{
          data: series.map(p => p.value / 100_000_000),
          borderColor: '#111',
          backgroundColor: 'rgba(0,0,0,0.04)',
          borderWidth: 2,
          fill: true,
          tension: 0.15,
          pointRadius: series.length > 15 ? 0 : 2,
          pointHoverRadius: 4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 0 },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) => `${(ctx.parsed.y).toLocaleString()}억`,
            },
          },
        },
        scales: {
          x: { ticks: { font: { size: 9 }, maxRotation: 0, maxTicksLimit: 6 } },
          y: { ticks: { font: { size: 9 }, callback: v => v.toLocaleString() + '억' } },
        },
      },
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
