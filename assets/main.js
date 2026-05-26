// ============================================
// 메인페이지 - 날짜별 index_*.json fetch & 동적 렌더링
// ============================================

(function() {
  'use strict';

  const groupsEl = document.getElementById('sector-groups');
  const loadingEl = document.getElementById('loading');
  const searchEl = document.getElementById('sector-search');
  const tradingDateEl = document.getElementById('trading-date');

  // ─────────────────────────────────────────────
  // 유틸
  // ─────────────────────────────────────────────
  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function formatDate(iso) {
    if (!iso) return '―';
    return iso.slice(0, 10).replace(/-/g, '.');
  }

  // URL ?date= 파라미터 읽기
  function getDateParam() {
    const p = new URLSearchParams(window.location.search);
    return p.get('date');
  }

  // URL에 ?date= 갱신 (history replace — 뒤로가기 안 쌓임)
  function setDateParam(date) {
    const url = new URL(window.location.href);
    if (date) url.searchParams.set('date', date);
    else url.searchParams.delete('date');
    window.history.replaceState({}, '', url.toString());
  }

  // ─────────────────────────────────────────────
  // 드롭다운 (tradingDateEl 자리에 동적 삽입)
  // ─────────────────────────────────────────────
  let datesMeta = null;       // {available_dates, latest}
  let currentDate = null;     // 현재 선택된 날짜

  function renderDateSelector(selectedDate) {
    if (!datesMeta || !datesMeta.available_dates || datesMeta.available_dates.length === 0) {
      tradingDateEl.textContent = '―';
      return;
    }
    const opts = datesMeta.available_dates.map(d =>
      `<option value="${escapeHtml(d)}"${d === selectedDate ? ' selected' : ''}>${formatDate(d)}</option>`
    ).join('');
    // 단일 날짜면 그냥 텍스트로 (선택 의미 없음)
    if (datesMeta.available_dates.length === 1) {
      tradingDateEl.textContent = formatDate(selectedDate);
      return;
    }
    tradingDateEl.innerHTML = `<select id="date-selector" class="date-selector">${opts}</select>`;
    const sel = document.getElementById('date-selector');
    sel.addEventListener('change', (e) => {
      const newDate = e.target.value;
      currentDate = newDate;
      setDateParam(newDate);
      loadIndex(newDate);
    });
  }

  // ─────────────────────────────────────────────
  // 그루핑/렌더링
  // ─────────────────────────────────────────────
  function groupSectors(sectors) {
    const out = {};
    for (const s of sectors) {
      const k1 = s.wics_1st;
      if (!out[k1]) out[k1] = { name: k1, sectors: [] };
      out[k1].sectors.push(s);
    }
    return Object.values(out);
  }

  function fmtRet(r) {
    if (r == null) return '';
    const cls = r >= 0 ? 'change-up' : 'change-down';
    const sign = r >= 0 ? '+' : '';
    return `<span class="card-return ${cls}">${sign}${r.toFixed(2)}%</span>`;
  }

  function renderCard(s, dateForLink) {
    const newBadge = s.issue_count_today > 0
      ? `<span class="card-new-dot" title="신규 ${s.issue_count_today}건"></span>`
      : '';
    const dataName = `${s.wics_3rd} ${s.wics_2nd} ${s.wics_1st} ${s.slug}`.toLowerCase();
    // 섹터 페이지로 갈 때 현재 날짜 전파
    const href = dateForLink
      ? `sectors/${encodeURIComponent(s.slug)}.html?date=${encodeURIComponent(dateForLink)}`
      : `sectors/${encodeURIComponent(s.slug)}.html`;
    return `
      <a href="${href}" class="card" data-name="${escapeHtml(dataName)}">
        <div class="card-path">${escapeHtml(s.wics_2nd)}</div>
        <div class="card-name">${escapeHtml(s.wics_3rd)}${newBadge}</div>
        ${fmtRet(s.sector_return)}
      </a>
    `;
  }

  function renderGroups(groups, dateForLink) {
    return groups.map(g => `
      <div class="group">
        <div class="group-header">
          <h2 class="group-title">${escapeHtml(g.name)}</h2>
          <span class="group-count">${g.sectors.length}</span>
        </div>
        <div class="card-grid">
          ${g.sectors.map(s => renderCard(s, dateForLink)).join('')}
        </div>
      </div>
    `).join('');
  }

  function bindSearch() {
    const cards = document.querySelectorAll('.card');
    const groups = document.querySelectorAll('.group');
    if (!searchEl) return;
    searchEl.value = '';   // 날짜 전환 시 검색어 리셋
    searchEl.addEventListener('input', (e) => {
      const q = e.target.value.trim().toLowerCase();
      cards.forEach(card => {
        const match = card.dataset.name.includes(q);
        card.style.display = match ? '' : 'none';
      });
      groups.forEach(group => {
        const visible = [...group.querySelectorAll('.card')].some(c => c.style.display !== 'none');
        group.style.display = visible ? '' : 'none';
      });
    });
  }

  // ─────────────────────────────────────────────
  // 날짜별 index fetch & 렌더
  // ─────────────────────────────────────────────
  async function loadIndex(date) {
    if (loadingEl) {
      loadingEl.classList.remove('error');
      loadingEl.textContent = '불러오는 중...';
      loadingEl.style.display = '';
    }
    groupsEl.innerHTML = '';
    try {
      const resp = await fetch(`data/index_${encodeURIComponent(date)}.json?_=${Date.now()}`);
      if (!resp.ok) throw new Error(`fetch failed: ${resp.status}`);
      const data = await resp.json();

      const groups = groupSectors(data.sectors || []);
      const order = ['IT', '커뮤니케이션서비스', '경기관련소비재', '필수소비재',
                     '건강관리', '금융', '산업재', '소재', '에너지', '유틸리티'];
      groups.sort((a, b) => {
        const ai = order.indexOf(a.name); const bi = order.indexOf(b.name);
        return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
      });

      groupsEl.innerHTML = renderGroups(groups, date);
      if (loadingEl) loadingEl.style.display = 'none';
      bindSearch();
    } catch (e) {
      console.error(e);
      if (loadingEl) {
        loadingEl.innerHTML = `데이터를 불러올 수 없습니다.<br><small>${escapeHtml(String(e))}</small>`;
        loadingEl.classList.add('error');
        loadingEl.style.display = '';
      }
    }
  }

  // ─────────────────────────────────────────────
  // 진입: dates.json → index 결정 → 렌더
  // ─────────────────────────────────────────────
  async function load() {
    try {
      // 1. dates.json fetch
      const resp = await fetch('data/dates.json?_=' + Date.now());
      if (!resp.ok) throw new Error(`dates.json fetch failed: ${resp.status}`);
      datesMeta = await resp.json();
      if (!datesMeta.available_dates || datesMeta.available_dates.length === 0) {
        throw new Error('가용 날짜 없음');
      }

      // 2. 선택할 날짜 결정 (URL 우선, 없으면 latest)
      const urlDate = getDateParam();
      if (urlDate && datesMeta.available_dates.includes(urlDate)) {
        currentDate = urlDate;
      } else {
        currentDate = datesMeta.latest;
        setDateParam(currentDate);   // 없거나 잘못된 값이면 URL도 동기화
      }

      // 3. 드롭다운 렌더
      renderDateSelector(currentDate);

      // 4. index 로드
      await loadIndex(currentDate);
    } catch (e) {
      console.error(e);
      if (loadingEl) {
        loadingEl.innerHTML = `데이터를 불러올 수 없습니다.<br><small>${escapeHtml(String(e))}</small>`;
        loadingEl.classList.add('error');
      }
    }
  }

  load();
})();
