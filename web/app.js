const state = {
  user: null,
  csrfToken: "",
  view: "library",
  books: [],
  series: [],
  recommendations: [],
  recommendationSeries: [],
  publishers: [],
  currentBook: null,
  currentSeries: null,
  pendingSeriesFollow: null,
  jobTimer: null,
  missingFilter: "",
  offset: 0,
  totalBooks: 0,
  pageSize: 100,
  seriesOffset: 0,
  totalSeries: 0,
  seriesPageSize: 60,
};

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const escapeHtml = (value = "") => String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[c]);
const safeUrl = (value = "") => /^https?:\/\//i.test(value) ? escapeHtml(value) : "";

async function api(path, options = {}) {
  const headers = {"Content-Type": "application/json", ...(options.headers || {})};
  if (state.csrfToken && options.method && options.method !== "GET") {
    headers["X-CSRF-Token"] = state.csrfToken;
  }
  const response = await fetch(path, {...options, headers});
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && path !== "/api/auth/me") showLogin();
  if (!response.ok) throw new Error(data.error || data.message || `HTTP ${response.status}`);
  return data;
}

function showLogin() {
  state.user = null;
  state.csrfToken = "";
  $("#loginGate").classList.remove("hidden");
}

function applyRoleUi() {
  const isAdmin = Boolean(state.user?.is_admin);
  const qualityNav = document.querySelector('[data-view="quality"]');
  if (qualityNav) qualityNav.classList.toggle("hidden", !isAdmin);
  const sourcesNav = document.querySelector('[data-view="sources"]');
  if (sourcesNav) sourcesNav.classList.toggle("hidden", !isAdmin);
  $("#updateButton").classList.toggle("hidden", !isAdmin);
  document.querySelectorAll('a[href="/api/export.json"], a[href="/api/export.csv"]').forEach(node => {
    node.classList.toggle("hidden", !isAdmin);
  });
  $("#accountMenu").classList.remove("hidden");
  $("#accountName").textContent = state.user.display_name || state.user.email;
  $("#accountRole").textContent = isAdmin ? "管理員" : "一般帳號";
  const avatar = $("#accountAvatar");
  avatar.src = state.user.avatar_url || "";
  avatar.classList.toggle("hidden", !state.user.avatar_url);
}

async function loadSession() {
  const session = await api("/api/auth/me");
  if (!session.authenticated) {
    const config = await api("/api/auth/config");
    $("#googleLogin").classList.toggle("hidden", !config.google_enabled);
    $("#loginUnavailable").classList.toggle("hidden", config.google_enabled);
    showLogin();
    return false;
  }
  state.user = session.user;
  state.csrfToken = session.user.csrf_token;
  $("#loginGate").classList.add("hidden");
  applyRoleUi();
  return true;
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.remove("hidden");
  clearTimeout(node.timer);
  node.timer = setTimeout(() => node.classList.add("hidden"), 4500);
}

function releaseLabel(book) {
  if (!book.release_date) return "日期未定";
  const [year, month, day] = book.release_date.split("-");
  return book.release_precision === "month" ? `${year}/${month}` : `${year}/${month}/${day}`;
}

const statusLabels = {scheduled: "預定出版", available: "已上市", delayed: "延期", cancelled: "取消", unknown: "資料缺日期"};
const typeLabels = {manga: "漫畫", novel: "輕小說", unknown: "未分類"};
const editionLabels = {standard: "一般版", special: "特裝版", limited: "限定版", deluxe: "豪華限定版", first_print: "首刷限定", bonus: "特典版", bundle: "同捆版", digital: "電子版", collector: "收藏版"};
const wishlistLabels = {wanted: "想買", preordered: "已預購", purchased: "已購入", paused: "暫不購買"};
const issueLabels = {missing_author: "缺作者", missing_isbn: "缺 ISBN", missing_date: "缺日期", unknown_type: "類型未定", suspicious_date: "可疑舊日期"};
const priorityLabels = ["一般", "稍高", "優先", "必買"];
const formatLabels = {paper: "紙本", digital: "電子書", both: "紙本＋電子"};

async function checkHealth() {
  try {
    const health = await api("/api/health");
    $("#healthDot").className = "health-dot ok";
    $("#healthText").textContent = `MySQL ${health.version}`;
    $("#setupBanner").classList.add("hidden");
    return true;
  } catch (error) {
    $("#healthDot").className = "health-dot bad";
    $("#healthText").textContent = "需要初始化";
    $("#setupMessage").textContent = error.message;
    $("#setupBanner").classList.remove("hidden");
    return false;
  }
}

async function loadAll() {
  if (!await loadSession()) return;
  if (!await checkHealth()) return;
  await Promise.all([loadPublishers(), loadStats(), loadBooks()]);
  if (state.user.is_admin) await loadLatestJob();
  return true;
}

async function loadStats() {
  const data = await api("/api/stats");
  $("#statTotal").textContent = data.total.toLocaleString();
  $("#statScheduled").textContent = data.scheduled.toLocaleString();
  $("#statAvailable").textContent = data.available.toLocaleString();
  $("#statUnknown").textContent = data.unknown.toLocaleString();
  $("#statScheduled").title = `${data.scheduled_undated.toLocaleString()} 本為官方預定、日期尚未公布`;
  $("#statWishlist").textContent = `${data.wishlist.toLocaleString()} / ${data.followed_series.toLocaleString()}`;
}

async function loadPublishers() {
  const data = await api("/api/publishers");
  state.publishers = data.items;
  const options = data.items
    .filter(item => item.enabled || item.book_count)
    .map(item => `<option value="${escapeHtml(item.code)}">${escapeHtml(item.name)}</option>`).join("");
  $("#publisherFilter").innerHTML = `<option value="">全部出版社</option>${options}`;
  renderSources();
}

function bookParams() {
  const params = new URLSearchParams();
  const q = $("#searchInput").value.trim();
  if (q) params.set("q", q);
  const mappings = [
    ["publisherFilter", "publisher"], ["typeFilter", "media_type"],
    ["statusFilter", "status"], ["editionFilter", "edition"],
    ["sortFilter", "sort"], ["wishlistStateFilter", "wishlist_state"],
    ["dateFromFilter", "date_from"], ["dateToFilter", "date_to"],
  ];
  mappings.forEach(([id, key]) => { if ($(`#${id}`).value) params.set(key, $(`#${id}`).value); });
  if (state.view === "wishlist") params.set("wishlist", "1");
  if (state.missingFilter) params.set("missing", state.missingFilter);
  params.set("limit", String(state.pageSize));
  params.set("offset", String(state.offset));
  return params;
}

async function loadBooks(resetPage = false) {
  if (resetPage) state.offset = 0;
  const data = await api(`/api/books?${bookParams()}`);
  state.books = data.items;
  state.totalBooks = data.total;
  const start = data.total ? state.offset + 1 : 0;
  const end = Math.min(state.offset + state.books.length, data.total);
  $("#resultCount").textContent = `共 ${data.total.toLocaleString()} 筆 · 顯示 ${start.toLocaleString()}–${end.toLocaleString()}`;
  renderPagination();
  renderBooks();
}

function renderPagination() {
  const page = Math.floor(state.offset / state.pageSize) + 1;
  const totalPages = Math.max(1, Math.ceil(state.totalBooks / state.pageSize));
  $("#pageIndicator").textContent = `第 ${page} / ${totalPages} 頁`;
  $("#previousPage").disabled = state.offset === 0;
  $("#nextPage").disabled = state.offset + state.pageSize >= state.totalBooks;
  $("#pagination").classList.toggle("hidden", state.totalBooks <= state.pageSize);
}

function renderBooks() {
  const grid = $("#bookGrid");
  $("#emptyState").classList.toggle("hidden", state.books.length !== 0);
  grid.innerHTML = state.books.map(book => {
    const cover = safeUrl(book.cover_url);
    const image = cover ? `<img src="${cover}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.remove()">` : "";
    const selected = Boolean(book.wishlist_state);
    const edition = book.edition_type !== "standard" ? `<span class="edition-chip">${escapeHtml(editionLabels[book.edition_type] || book.edition_type)}</span>` : "";
    const priority = selected && Number(book.wishlist_priority) > 0 ? `<span class="priority-chip p${book.wishlist_priority}">${priorityLabels[book.wishlist_priority]}</span>` : "";
    return `<article class="book-card" data-id="${book.id}" tabindex="0">
      <div class="cover">${image}
        <span class="badge ${escapeHtml(book.release_status)}">${escapeHtml(statusLabels[book.release_status] || "未知")}</span>
        <button class="heart ${selected ? "selected" : ""}" data-wishlist="${book.id}" aria-label="${selected ? "移出" : "加入"}訂選清單">${selected ? "♥" : "♡"}</button>
      </div>
      <div class="book-meta">
        <div class="chip-row">${edition}${priority}</div>
        <h3>${escapeHtml(book.title)}</h3>
        <p>${escapeHtml(book.author || "作者未提供")} · ${escapeHtml(book.publisher_name)}</p>
        <div class="release-row"><time>${releaseLabel(book)}</time><span>${escapeHtml(typeLabels[book.media_type] || "未分類")}</span></div>
      </div>
    </article>`;
  }).join("");
  $$(".book-card").forEach(card => {
    card.addEventListener("click", event => {
      if (!event.target.closest("[data-wishlist]")) openDetail(Number(card.dataset.id));
    });
    card.addEventListener("keydown", event => { if (event.key === "Enter") openDetail(Number(card.dataset.id)); });
  });
  $$("[data-wishlist]").forEach(button => button.addEventListener("click", event => {
    event.stopPropagation();
    quickWishlist(Number(button.dataset.wishlist));
  }));
}

async function quickWishlist(bookId) {
  const book = state.books.find(item => item.id === bookId);
  try {
    if (book?.wishlist_state) {
      await api(`/api/wishlist/${bookId}`, {method: "DELETE"});
      toast("已移出訂選清單");
    } else {
      await api(`/api/wishlist/${bookId}`, {method: "POST", body: JSON.stringify({state: "wanted"})});
      toast("已加入訂選清單");
    }
    await Promise.all([loadBooks(), loadStats()]);
  } catch (error) { toast(error.message, true); }
}

async function openDetail(bookId) {
  try {
    const [book, relatedData] = await Promise.all([
      api(`/api/books/${bookId}`),
      api(`/api/books/${bookId}/recommendations?limit=8`),
    ]);
    const related = relatedData.items || [];
    state.currentBook = book;
    const cover = safeUrl(book.cover_url);
    const source = safeUrl(book.source_url);
    $("#detailContent").innerHTML = `<div class="detail">
      <div class="detail-cover">${cover ? `<img src="${cover}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}</div>
      <div class="detail-body">
        <p class="eyebrow">${escapeHtml(book.publisher_name)} · ${escapeHtml(typeLabels[book.media_type] || "未分類")}</p>
        <h2>${escapeHtml(book.title)}</h2>
        <p class="detail-sub">${escapeHtml(book.author || "作者未提供")}</p>
        <button class="series-link" id="openBookSeries">${escapeHtml(book.series_title || "未辨識系列")} · ${escapeHtml(book.volume_label || "單冊")}</button>
        <div class="detail-grid">
          <div><small>上市日期</small><strong>${releaseLabel(book)}</strong></div>
          <div><small>狀態</small><strong>${escapeHtml(statusLabels[book.release_status] || "未知")}</strong></div>
          <div><small>版本</small><strong>${escapeHtml(editionLabels[book.edition_type] || book.edition_type)}</strong></div>
          <div><small>ISBN</small><strong>${escapeHtml(book.isbn || "未提供")}</strong></div>
          <div><small>定價</small><strong>${book.list_price ? `NT$ ${Number(book.list_price).toLocaleString()}` : "未提供"}</strong></div>
          <div><small>首次收錄</small><strong>${escapeHtml((book.first_seen_at || "").replace("T", " ").slice(0, 16))}</strong></div>
        </div>
        <form class="wishlist-form" id="wishlistForm">
          <div class="form-grid">
            <label>訂選狀態<select id="wishlistState">${Object.entries(wishlistLabels).map(([key, value]) => `<option value="${key}" ${book.wishlist_state === key ? "selected" : ""}>${value}</option>`).join("")}</select></label>
            <label>優先度<select id="wishlistPriority">${priorityLabels.map((label, index) => `<option value="${index}" ${Number(book.wishlist_priority || 0) === index ? "selected" : ""}>${label}</option>`).join("")}</select></label>
            <label>收藏格式<select id="wishlistFormat">${Object.entries(formatLabels).map(([key, value]) => `<option value="${key}" ${book.wishlist_format === key ? "selected" : ""}>${value}</option>`).join("")}</select></label>
            <label class="check-label"><input id="followSeries" type="checkbox" ${book.follow_series || book.series_following ? "checked" : ""}> 自動追蹤此系列新書</label>
            <label>預購／購入店家<input id="wishlistStore" value="${escapeHtml(book.wishlist_store || "")}" placeholder="例如博客來、安利美特"></label>
            <label>訂單編號<input id="wishlistOrder" value="${escapeHtml(book.wishlist_order_number || "")}" placeholder="僅儲存在本機"></label>
            <label>實付價格<input id="wishlistPaidPrice" type="number" min="0" value="${escapeHtml(book.wishlist_paid_price || "")}" placeholder="NT$"></label>
          </div>
          <label>備註<textarea id="wishlistNotes" placeholder="版本、取貨資訊或其他備註…">${escapeHtml(book.wishlist_notes || "")}</textarea></label>
          <div class="form-actions"><button class="primary" type="submit">${book.wishlist_state ? "儲存訂選" : "加入訂選清單"}</button>${book.wishlist_state ? '<button class="danger" type="button" id="removeWishlist">移除</button>' : ""}</div>
        </form>
        ${book.history?.length ? `<div class="history-list"><h3>日期與狀態異動</h3>${book.history.slice(0, 8).map(item => `<div><time>${escapeHtml((item.observed_at || "").replace("T", " ").slice(0, 16))}</time><span>${escapeHtml(item.field_name)}：${escapeHtml(item.old_value || "無")} → ${escapeHtml(item.new_value || "無")}</span></div>`).join("")}</div>` : ""}
        ${source ? `<a class="source-link" href="${source}" target="_blank" rel="noopener">查看出版社原始頁面 ↗</a>` : ""}
      </div>
      ${related.length ? `<section class="detail-related">
        <div class="detail-related-heading">
          <div><p class="eyebrow">YOU MAY ALSO LIKE</p><h3>你可能會喜歡</h3></div>
          <small>同系列優先，其次為同作者作品</small>
        </div>
        <div class="detail-related-track">${related.map(item => {
          const relatedCover = safeUrl(item.cover_url);
          return `<article class="detail-related-card" data-related-book="${item.id}">
            <div class="detail-related-cover">${relatedCover ? `<img src="${relatedCover}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.remove()">` : "<span>A</span>"}</div>
            <div class="detail-related-info">
              <span class="recommendation-chip">${recommendationLabel(item)}</span>
              <h4>${escapeHtml(item.title)}</h4>
              <p>${escapeHtml(item.recommendation_reason)}</p>
              <button class="text-button related-want" data-related-want="${item.id}">＋ 加入想買</button>
            </div>
          </article>`;
        }).join("")}</div>
      </section>` : ""}
    </div>`;
    $("#wishlistForm").addEventListener("submit", saveWishlist);
    $("#removeWishlist")?.addEventListener("click", removeWishlist);
    $("#openBookSeries").addEventListener("click", () => {
      $("#detailDialog").close();
      openSeriesByValues(book.publisher_code, book.series_title);
    });
    $$("[data-related-book]").forEach(card => card.addEventListener("click", event => {
      if (!event.target.closest("[data-related-want]")) openDetail(Number(card.dataset.relatedBook));
    }));
    $$("[data-related-want]").forEach(button => button.addEventListener("click", event => {
      event.stopPropagation();
      addRelatedToWishlist(Number(button.dataset.relatedWant), book.id);
    }));
    $("#detailDialog").scrollTop = 0;
    if (!$("#detailDialog").open) $("#detailDialog").showModal();
  } catch (error) { toast(error.message, true); }
}

async function addRelatedToWishlist(bookId, currentBookId) {
  try {
    await api(`/api/wishlist/${bookId}`, {
      method: "POST",
      body: JSON.stringify({state: "wanted"}),
    });
    toast("已加入想買");
    await Promise.all([loadBooks(), loadStats()]);
    await openDetail(currentBookId);
  } catch (error) { toast(error.message, true); }
}

async function saveWishlist(event) {
  event.preventDefault();
  const book = state.currentBook;
  const becamePurchased = book.wishlist_state !== "purchased" && $("#wishlistState").value === "purchased";
  const shouldSuggestFollow = becamePurchased && book.series_title
    && !book.series_following && !$("#followSeries").checked;
  const payload = {
    state: $("#wishlistState").value,
    notes: $("#wishlistNotes").value,
    follow_series: $("#followSeries").checked,
    priority: Number($("#wishlistPriority").value),
    store_name: $("#wishlistStore").value,
    order_number: $("#wishlistOrder").value,
    paid_price: $("#wishlistPaidPrice").value,
    owned_format: $("#wishlistFormat").value,
  };
  try {
    await api(`/api/wishlist/${state.currentBook.id}`, {method: "POST", body: JSON.stringify(payload)});
    $("#detailDialog").close();
    toast(payload.follow_series ? "訂選已儲存，並開始追蹤系列" : "訂選清單已儲存");
    await Promise.all([loadBooks(), loadStats()]);
    if (shouldSuggestFollow) showSeriesFollowPrompt(book);
  } catch (error) { toast(error.message, true); }
}

async function removeWishlist() {
  try {
    await api(`/api/wishlist/${state.currentBook.id}`, {method: "DELETE"});
    $("#detailDialog").close();
    toast("已移出訂選清單");
    await Promise.all([loadBooks(), loadStats()]);
  } catch (error) { toast(error.message, true); }
}

async function loadSeries(resetPage = false) {
  if (resetPage) state.seriesOffset = 0;
  const params = new URLSearchParams({
    limit: String(state.seriesPageSize),
    offset: String(state.seriesOffset),
  });
  const q = $("#searchInput").value.trim();
  if (q) params.set("q", q);
  if ($("#publisherFilter").value) params.set("publisher", $("#publisherFilter").value);
  const data = await api(`/api/series?${params}`);
  state.series = data.items;
  state.totalSeries = data.total;
  const start = data.total ? state.seriesOffset + 1 : 0;
  const end = Math.min(state.seriesOffset + state.series.length, data.total);
  $("#seriesCount").textContent = `共 ${data.total.toLocaleString()} 個系列 · 顯示 ${start}–${end}`;
  const page = Math.floor(state.seriesOffset / state.seriesPageSize) + 1;
  const pages = Math.max(1, Math.ceil(state.totalSeries / state.seriesPageSize));
  $("#seriesPageIndicator").textContent = `第 ${page} / ${pages} 頁`;
  $("#previousSeriesPage").disabled = state.seriesOffset === 0;
  $("#nextSeriesPage").disabled = state.seriesOffset + state.seriesPageSize >= state.totalSeries;
  $("#seriesPagination").classList.toggle("hidden", state.totalSeries <= state.seriesPageSize);
  renderSeries();
}

function renderSeries() {
  $("#seriesGrid").innerHTML = state.series.map((series, index) => {
    const cover = safeUrl(series.cover_url);
    return `<article class="series-card" data-series-index="${index}" tabindex="0">
      <div class="series-cover">${cover ? `<img src="${cover}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.remove()">` : "<span>A</span>"}</div>
      <div class="series-info">
        <p>${escapeHtml(series.publisher_name)} · ${escapeHtml(typeLabels[series.media_type] || "未分類")}</p>
        <h3>${escapeHtml(series.series_title)}</h3>
        <div class="series-stats"><span>${series.book_count} 個版本</span><span>${series.volume_count} 個集數</span><span>${series.scheduled_count} 本預定</span></div>
        <button class="follow-button ${series.is_following ? "following" : ""}" data-follow-index="${index}">${series.is_following ? "✓ 已追蹤" : "＋ 追蹤系列"}</button>
      </div>
    </article>`;
  }).join("");
  $$("[data-series-index]").forEach(card => {
    card.addEventListener("click", event => {
      if (!event.target.closest("[data-follow-index]")) openSeries(Number(card.dataset.seriesIndex));
    });
    card.addEventListener("keydown", event => { if (event.key === "Enter") openSeries(Number(card.dataset.seriesIndex)); });
  });
  $$("[data-follow-index]").forEach(button => button.addEventListener("click", event => {
    event.stopPropagation();
    toggleSeriesFollow(Number(button.dataset.followIndex));
  }));
}

async function toggleSeriesFollow(index) {
  const series = state.series[index];
  try {
    await api("/api/series/follow", {
      method: "POST",
      body: JSON.stringify({
        publisher: series.publisher_code,
        series_title: series.series_title,
        media_type: series.media_type,
        following: !series.is_following,
      }),
    });
    toast(series.is_following ? "已停止追蹤系列" : "已開始追蹤系列，新書會自動加入想買");
    await Promise.all([loadSeries(), loadStats()]);
  } catch (error) { toast(error.message, true); }
}

function openSeries(index) {
  const series = state.series[index];
  openSeriesByValues(series.publisher_code, series.series_title);
}

async function openSeriesByValues(publisher, title) {
  if (!title) return;
  try {
    const params = new URLSearchParams({publisher, title});
    const series = await api(`/api/series/detail?${params}`);
    state.currentSeries = series;
    $("#seriesDetailContent").innerHTML = `<div class="series-detail">
      <p class="eyebrow">${escapeHtml(series.publisher_name)} · ${escapeHtml(typeLabels[series.media_type] || "未分類")}</p>
      <h2>${escapeHtml(series.series_title)}</h2>
      <p class="detail-sub">同一系列的集數、一般版與限定版集中顯示。</p>
      ${series.missing_volumes?.length ? `<div class="missing-volumes">可能缺少集數：${series.missing_volumes.map(String).join("、")}</div>` : ""}
      <div class="series-book-list">${series.items.map(book => `<button class="series-book-row" data-series-book="${book.id}">
        <span class="volume-box">${escapeHtml(book.volume_label || "—")}</span>
        <span><strong>${escapeHtml(book.title)}</strong><small>${escapeHtml(editionLabels[book.edition_type] || book.edition_type)} · ${releaseLabel(book)}</small></span>
        <span class="${book.wishlist_state ? "owned-state active" : "owned-state"}">${book.wishlist_state ? escapeHtml(wishlistLabels[book.wishlist_state]) : "未訂選"}</span>
      </button>`).join("")}</div>
    </div>`;
    $$("[data-series-book]").forEach(button => button.addEventListener("click", () => {
      $("#seriesDialog").close();
      openDetail(Number(button.dataset.seriesBook));
    }));
    $("#seriesDialog").showModal();
  } catch (error) { toast(error.message, true); }
}

async function loadUpcoming() {
  const days = Number($("#upcomingDays").value);
  const data = await api(`/api/upcoming?days=${days}`);
  const groups = Object.groupBy ? Object.groupBy(data.items, item => item.release_date || "unknown") :
    data.items.reduce((result, item) => ((result[item.release_date || "unknown"] ||= []).push(item), result), {});
  $("#upcomingList").innerHTML = Object.entries(groups).map(([releaseDate, books]) => `<section class="timeline-day">
    <div class="timeline-date"><strong>${escapeHtml(releaseDate.replaceAll("-", "/"))}</strong><small>${books.length} 本</small></div>
    <div class="timeline-books">${books.map(book => `<button class="timeline-book" data-upcoming-book="${book.id}">
      <span><strong>${escapeHtml(book.title)}</strong><small>${escapeHtml(book.publisher_name)} · ${escapeHtml(book.author || "作者未提供")}</small></span>
      <span>${book.wishlist_state ? `♥ ${escapeHtml(wishlistLabels[book.wishlist_state])}` : escapeHtml(typeLabels[book.media_type])}</span>
    </button>`).join("")}</div>
  </section>`).join("") || '<div class="empty"><span>◷</span><h2>這段期間沒有預定書目</h2></div>';
  $$("[data-upcoming-book]").forEach(button => button.addEventListener("click", () => openDetail(Number(button.dataset.upcomingBook))));
  return data.items;
}

async function loadRecommendations() {
  const data = await api("/api/recommendations?limit=60");
  state.recommendations = data.items;
  state.recommendationSeries = data.series_prompts;
  renderRecommendations(data.purchased_count, data.followed_count);
}

function recommendationLabel(book) {
  if (book.recommendation_types.includes("same_series") || book.recommendation_types.includes("book_series")) return "同系列";
  if (book.recommendation_types.includes("followed_series")) return "追蹤系列";
  return "同作者";
}

function renderRecommendations(purchasedCount, followedCount) {
  $("#seriesPrompts").innerHTML = state.recommendationSeries.map((series, index) => `<article class="series-prompt">
    <div>
      <p class="eyebrow">追蹤建議</p>
      <h3>${escapeHtml(series.series_title)}</h3>
      <p>你已購入 ${Number(series.purchased_count)} 本；資料庫內有 ${Number(series.book_count)} 個版本${Number(series.scheduled_count) ? `，其中 ${Number(series.scheduled_count)} 本預定出版` : ""}。</p>
    </div>
    <div class="prompt-actions">
      <button class="primary compact" data-recommend-follow="${index}">追蹤系列</button>
      <button class="secondary compact" data-recommend-series="${index}">查看系列</button>
    </div>
  </article>`).join("");
  $("#recommendationGrid").innerHTML = state.recommendations.map((book, index) => {
    const cover = safeUrl(book.cover_url);
    return `<article class="recommendation-card" data-recommend-book="${index}">
      <div class="recommendation-cover">${cover ? `<img src="${cover}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.remove()">` : "<span>A</span>"}</div>
      <div class="recommendation-body">
        <div class="chip-row">
          <span class="recommendation-chip">${recommendationLabel(book)}</span>
          ${book.release_status === "scheduled" ? '<span class="edition-chip">預定出版</span>' : ""}
        </div>
        <h3>${escapeHtml(book.title)}</h3>
        <p class="recommendation-meta">${escapeHtml(book.author || "作者未提供")} · ${escapeHtml(book.publisher_name)}</p>
        <p class="recommendation-reason">${escapeHtml(book.recommendation_reason)}</p>
        <div class="recommendation-actions">
          <button class="primary compact" data-recommend-want="${index}">加入想買</button>
          <button class="secondary compact" data-recommend-detail="${index}">查看</button>
          <button class="text-button" data-recommend-dismiss="${index}">忽略</button>
        </div>
      </div>
    </article>`;
  }).join("");
  const empty = !state.recommendations.length && !state.recommendationSeries.length;
  $("#recommendationEmpty").classList.toggle("hidden", !empty);
  $("#recommendationEmptyText").textContent = purchasedCount || followedCount
    ? "目前找不到尚未訂選的同系列或同作者作品；之後更新到新書時會自動出現在這裡。"
    : "先將收藏中的書標記為「已購入」或追蹤一個系列，系統就會開始整理推薦。";
  $$("[data-recommend-book]").forEach(card => card.addEventListener("click", event => {
    if (!event.target.closest("button")) openDetail(state.recommendations[Number(card.dataset.recommendBook)].id);
  }));
  $$("[data-recommend-detail]").forEach(button => button.addEventListener("click", () => {
    openDetail(state.recommendations[Number(button.dataset.recommendDetail)].id);
  }));
  $$("[data-recommend-want]").forEach(button => button.addEventListener("click", () => {
    addRecommendationToWishlist(Number(button.dataset.recommendWant));
  }));
  $$("[data-recommend-dismiss]").forEach(button => button.addEventListener("click", () => {
    dismissRecommendation(Number(button.dataset.recommendDismiss));
  }));
  $$("[data-recommend-follow]").forEach(button => button.addEventListener("click", () => {
    followRecommendedSeries(Number(button.dataset.recommendFollow));
  }));
  $$("[data-recommend-series]").forEach(button => button.addEventListener("click", () => {
    const series = state.recommendationSeries[Number(button.dataset.recommendSeries)];
    openSeriesByValues(series.publisher_code, series.series_title);
  }));
}

async function addRecommendationToWishlist(index) {
  const book = state.recommendations[index];
  try {
    await api(`/api/wishlist/${book.id}`, {
      method: "POST",
      body: JSON.stringify({state: "wanted"}),
    });
    toast(`已將《${book.title}》加入想買`);
    await Promise.all([loadRecommendations(), loadStats()]);
  } catch (error) { toast(error.message, true); }
}

async function dismissRecommendation(index) {
  const book = state.recommendations[index];
  try {
    await api("/api/recommendations/dismiss", {
      method: "POST",
      body: JSON.stringify({book_id: book.id}),
    });
    toast(`已忽略《${book.title}》`);
    await loadRecommendations();
  } catch (error) { toast(error.message, true); }
}

async function followRecommendedSeries(index) {
  const series = state.recommendationSeries[index];
  try {
    await api("/api/series/follow", {
      method: "POST",
      body: JSON.stringify({
        publisher: series.publisher_code,
        series_title: series.series_title,
        media_type: series.media_type,
        following: true,
      }),
    });
    toast("已開始追蹤系列，新出版書目會自動加入想買");
    await Promise.all([loadRecommendations(), loadStats()]);
  } catch (error) { toast(error.message, true); }
}

function showSeriesFollowPrompt(book) {
  state.pendingSeriesFollow = book;
  $("#followPromptText").textContent = `你剛將《${book.title}》標成已購入。追蹤「${book.series_title}」後，新出版的同系列書目會自動加入想買。`;
  $("#followPromptDialog").showModal();
}

async function acceptSeriesFollowPrompt() {
  const book = state.pendingSeriesFollow;
  if (!book) return;
  try {
    await api("/api/series/follow", {
      method: "POST",
      body: JSON.stringify({
        publisher: book.publisher_code,
        series_title: book.series_title,
        media_type: book.media_type,
        following: true,
      }),
    });
    $("#followPromptDialog").close();
    state.pendingSeriesFollow = null;
    toast("已開始追蹤系列，新書會自動加入想買");
    await Promise.all([loadBooks(), loadStats()]);
    if (state.view === "recommendations") await loadRecommendations();
  } catch (error) { toast(error.message, true); }
}

async function enableNotifications() {
  if (!("Notification" in window)) return toast("這個瀏覽器不支援通知", true);
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return toast("未啟用瀏覽器通知");
  const items = await loadUpcoming();
  const selected = items.filter(item => item.wishlist_state).slice(0, 5);
  new Notification("AniShelf 上市提醒已啟用", {
    body: selected.length ? `未來有 ${selected.length} 本訂選書籍即將上市` : "未來上市資料會在「近期上市」顯示",
  });
  localStorage.setItem("anishelf-notifications", "1");
  toast("瀏覽器提醒已啟用");
}

async function loadQuality() {
  const data = await api("/api/quality");
  $("#qualitySummary").innerHTML = Object.entries(data.counts).map(([key, count]) => `<button class="quality-card" data-quality-filter="${key}">
    <strong>${Number(count).toLocaleString()}</strong><span>${escapeHtml(issueLabels[key])}</span>
  </button>`).join("");
  $("#qualityList").innerHTML = data.items.map(book => `<button class="quality-row" data-quality-book="${book.id}">
    <span><strong>${escapeHtml(book.title)}</strong><small>${escapeHtml(book.publisher_name)} · ${escapeHtml(book.release_date || "日期未定")}</small></span>
    <span class="issue-chips">${String(book.issues || "").split(",").filter(Boolean).map(issue => `<i>${escapeHtml(issueLabels[issue] || issue)}</i>`).join("")}</span>
  </button>`).join("");
  $$("[data-quality-book]").forEach(button => button.addEventListener("click", () => openDetail(Number(button.dataset.qualityBook))));
  $$("[data-quality-filter]").forEach(button => button.addEventListener("click", () => {
    const map = {missing_author: "author", missing_isbn: "isbn", missing_date: "date", unknown_type: "type", suspicious_date: "suspicious_date"};
    state.missingFilter = map[button.dataset.qualityFilter];
    switchView("library");
  }));
}

function renderSources() {
  $("#sourceList").innerHTML = state.publishers.map(source => `<article class="source-row">
    <div class="source-name"><strong>${escapeHtml(source.name)}</strong><small>${escapeHtml(source.homepage_url)}</small></div>
    <span class="source-state ${source.last_error ? "error" : ""}">${source.last_error ? "需檢查" : "正常"}</span>
    <span>${Number(source.book_count || 0).toLocaleString()} 本</span>
    <div class="source-sync">
      <time>${source.sync_success_at ? `上次增量 ${escapeHtml(source.sync_success_at.replace("T", " ").slice(0,16))}` : "尚未更新"}</time>
      <small>日期游標 ${escapeHtml(source.cursor_date || "未建立")} · ${source.backfill_completed ? "歷史回填完成" : "尚未回填"}</small>
    </div>
  </article>`).join("");
}

async function startUpdate() {
  const button = $("#updateButton");
  button.disabled = true;
  button.innerHTML = "<span>↻</span>更新中…";
  try {
    const job = await api("/api/update", {method: "POST", body: JSON.stringify({source: "all", force: true})});
    toast("已開始增量更新六家出版社");
    pollJob(job.job_id);
  } catch (error) {
    button.disabled = false;
    button.innerHTML = "<span>↻</span>更新資料";
    toast(error.message, true);
  }
}

function pollJob(jobId) {
  clearInterval(state.jobTimer);
  state.jobTimer = setInterval(async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (["completed", "partial", "failed"].includes(job.status)) {
        clearInterval(state.jobTimer);
        state.jobTimer = null;
        $("#updateButton").disabled = false;
        $("#updateButton").innerHTML = "<span>↻</span>更新資料";
        const summary = `發現 ${job.discovered_count}、新增 ${job.inserted_count}、修改 ${job.updated_count}`;
        toast(job.status === "completed" ? `更新完成：${summary}` : `更新部分完成：${summary}`, job.status === "failed");
        await Promise.all([loadBooks(), loadStats(), loadPublishers()]);
        if (state.view === "series") await loadSeries();
        if (state.view === "quality") await loadQuality();
        if (state.view === "recommendations") await loadRecommendations();
      }
    } catch (error) {
      clearInterval(state.jobTimer);
      state.jobTimer = null;
      toast(error.message, true);
    }
  }, 1200);
}

async function loadLatestJob() {
  const job = await api("/api/jobs/latest");
  if (job.id && ["queued", "running"].includes(job.status)) {
    $("#updateButton").disabled = true;
    $("#updateButton").innerHTML = "<span>↻</span>更新中…";
    pollJob(job.id);
  }
}

function clearFilters(reload = true) {
  ["publisherFilter", "typeFilter", "statusFilter", "editionFilter", "sortFilter", "wishlistStateFilter", "dateFromFilter", "dateToFilter"].forEach(id => $(`#${id}`).value = "");
  $("#searchInput").value = "";
  state.missingFilter = "";
  if (reload) loadBooks(true).catch(error => toast(error.message, true));
}

function switchView(view) {
  const availableViews = ["library", "wishlist", "series", "upcoming", "recommendations", "quality", "sources"];
  if (!availableViews.includes(view)) view = "library";
  if (!state.user?.is_admin && ["quality", "sources"].includes(view)) view = "library";
  state.view = view;
  if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
  $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === view));
  $$(".view-section").forEach(section => section.classList.add("hidden"));
  const libraryMode = ["library", "wishlist"].includes(view);
  $(`#${libraryMode ? "library" : view}View`).classList.remove("hidden");
  const titles = {
    library: ["MY COLLECTION", "Library"], wishlist: ["WISHLIST", "已訂選清單"],
    series: ["SERIES SHELF", "系列書架"], upcoming: ["RELEASE CALENDAR", "近期上市"],
    recommendations: ["FOR YOU", "為你推薦"],
    quality: ["DATA CHECK", "資料品質"], sources: ["SOURCE HEALTH", "資料來源"],
  };
  $("#pageEyebrow").textContent = titles[view][0];
  $("#pageTitle").textContent = titles[view][1];
  $("#searchBox").classList.toggle("hidden", !["library", "wishlist", "series"].includes(view));
  $("#wishlistStateFilter").classList.toggle("hidden", view !== "wishlist");
  if (libraryMode) loadBooks(true).catch(error => toast(error.message, true));
  if (view === "series") loadSeries(true).catch(error => toast(error.message, true));
  if (view === "upcoming") loadUpcoming().catch(error => toast(error.message, true));
  if (view === "recommendations") loadRecommendations().catch(error => toast(error.message, true));
  if (view === "quality") loadQuality().catch(error => toast(error.message, true));
}

let searchTimer;
$("#searchInput").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    const load = state.view === "series" ? () => loadSeries(true) : () => loadBooks(true);
    load().catch(error => toast(error.message, true));
  }, 280);
});
["publisherFilter", "typeFilter", "statusFilter", "editionFilter", "sortFilter", "wishlistStateFilter", "dateFromFilter", "dateToFilter"].forEach(id => {
  $(`#${id}`).addEventListener("change", () => {
    const load = state.view === "series" ? () => loadSeries(true) : () => loadBooks(true);
    load().catch(error => toast(error.message, true));
  });
});
$$(".nav-item").forEach(item => item.addEventListener("click", () => switchView(item.dataset.view)));
$("#clearFilters").addEventListener("click", () => clearFilters());
$("#upcomingDays").addEventListener("change", () => loadUpcoming().catch(error => toast(error.message, true)));
$("#notifyButton").addEventListener("click", () => enableNotifications().catch(error => toast(error.message, true)));
$("#restoreRecommendations").addEventListener("click", async () => {
  try {
    await api("/api/recommendations/dismissals", {method: "DELETE"});
    toast("已重新顯示忽略過的推薦");
    await loadRecommendations();
  } catch (error) { toast(error.message, true); }
});
$("#previousPage").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.pageSize);
  loadBooks().then(() => window.scrollTo({top: 0, behavior: "smooth"})).catch(error => toast(error.message, true));
});
$("#nextPage").addEventListener("click", () => {
  if (state.offset + state.pageSize < state.totalBooks) state.offset += state.pageSize;
  loadBooks().then(() => window.scrollTo({top: 0, behavior: "smooth"})).catch(error => toast(error.message, true));
});
$("#previousSeriesPage").addEventListener("click", () => {
  state.seriesOffset = Math.max(0, state.seriesOffset - state.seriesPageSize);
  loadSeries().then(() => window.scrollTo({top: 0, behavior: "smooth"})).catch(error => toast(error.message, true));
});
$("#nextSeriesPage").addEventListener("click", () => {
  if (state.seriesOffset + state.seriesPageSize < state.totalSeries) state.seriesOffset += state.seriesPageSize;
  loadSeries().then(() => window.scrollTo({top: 0, behavior: "smooth"})).catch(error => toast(error.message, true));
});
$("#updateButton").addEventListener("click", startUpdate);
$("#logoutButton").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", {method: "POST", body: "{}"});
  } finally {
    location.assign("/");
  }
});
$("#dialogClose").addEventListener("click", () => $("#detailDialog").close());
$("#detailDialog").addEventListener("click", event => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});
$("#seriesDialogClose").addEventListener("click", () => $("#seriesDialog").close());
$("#followPromptAccept").addEventListener("click", () => acceptSeriesFollowPrompt());
$("#followPromptLater").addEventListener("click", () => {
  $("#followPromptDialog").close();
  state.pendingSeriesFollow = null;
});
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    $("#searchInput").focus();
  }
});

const initialView = location.hash.slice(1);
loadAll()
  .then(loaded => {
    if (loaded && initialView && initialView !== "library") switchView(initialView);
  })
  .catch(error => toast(error.message, true));
