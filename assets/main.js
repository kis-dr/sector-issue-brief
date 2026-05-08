// ============================================
// 메인페이지 - index.json fetch & 동적 렌더링
// ============================================

(function() {
  'use strict';

  const groupsEl = document.getElementById('sector-groups');
  const loadingEl = document.getElementById('loading');
  const searchEl = document.getElementById('sector-search');
  const tradingDateEl = document.getElementById('trading-date');

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

  // 1st level → 2nd level → sectors 그루핑
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

  function renderCard(s) {
    const newBadge = s.issue_count_today > 0
      ? `<span class="card-new-dot" title="신규 ${s.issue_count_today}건"></span>`
      : '';
    const dataName = `${s.wics_3rd} ${s.wics_2nd} ${s.wics_1st} ${s.slug}`.toLowerCase();
    return `
      <a href="sectors/${escapeHtml(s.slug)}.html" class="card" data-name="${escapeHtml(dataName)}">
        <div class="card-path">${escapeHtml(s.wics_2nd)}</div>
        <div class="card-name">${escapeHtml(s.wics_3rd)}${newBadge}</div>
        ${fmtRet(s.sector_return)}
      </a>
    `;
  }

  function renderGroups(groups) {
    return groups.map(g => `
      <div class="group">
        <div class="group-header">
          <h2 class="group-title">${escapeHtml(g.name)}</h2>
          <span class="group-count">${g.sectors.length}</span>
        </div>
        <div class="card-grid">
          ${g.sectors.map(renderCard).join('')}
        </div>
      </div>
    `).join('');
  }

  function bindSearch() {
    const cards = document.querySelectorAll('.card');
    const groups = document.querySelectorAll('.group');
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

  async function load() {
    try {
      const resp = await fetch('data/index.json?_=' + Date.now());
      if (!resp.ok) throw new Error(`fetch failed: ${resp.status}`);
      const data = await resp.json();

      tradingDateEl.textContent = formatDate(data.trading_date);

      const groups = groupSectors(data.sectors || []);
      // 1st level 정렬: 미리 정해진 순서
      const order = ['IT', '커뮤니케이션서비스', '경기관련소비재', '필수소비재',
                     '건강관리', '금융', '산업재', '소재', '에너지', '유틸리티'];
      groups.sort((a, b) => {
        const ai = order.indexOf(a.name); const bi = order.indexOf(b.name);
        return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
      });

      groupsEl.innerHTML = renderGroups(groups);
      bindSearch();
    } catch (e) {
      console.error(e);
      loadingEl.innerHTML = `데이터를 불러올 수 없습니다.<br><small>${escapeHtml(String(e))}</small>`;
      loadingEl.classList.add('error');
    }
  }

  load();
})();
