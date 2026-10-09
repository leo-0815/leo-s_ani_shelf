/* Shared by cloud and local UI. All personal data comes from the active session. */
window.AniShelfHome = (() => {
  let generation = 0;
  const node = id => document.getElementById(id);
  const escape = value => String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[c]);

  function init({search}) {
    node("homeSearchForm").addEventListener("submit", event => {
      event.preventDefault();
      search(node("homeSearchInput").value.trim());
    });
  }

  async function load({api, user, openBook}) {
    const current = ++generation;
    node("homeGreeting").textContent = user?.display_name ? `${user.display_name}，歡迎回到書架。` : "歡迎回到你的書架。";
    ["homeWishlist", "homeCollection", "homeFollowed"].forEach(id => node(id).textContent = "—");
    node("homeUpcoming").innerHTML = '<p class="home-message" role="status">正在整理你關注的上市資訊…</p>';
    try {
      const data = await api("/api/home");
      if (current !== generation) return;
      node("homeWishlist").textContent = data.counts.wishlist.toLocaleString();
      node("homeCollection").textContent = data.counts.collection.toLocaleString();
      node("homeFollowed").textContent = data.counts.followed_series.toLocaleString();
      node("homeDate").textContent = `台北日期 ${data.date.replaceAll("-", "/")} · 未來 7 天`;
      if (!data.items.length) {
        node("homeUpcoming").innerHTML = '<div class="home-empty"><span aria-hidden="true">◷</span><h3>最近沒有你關注的書預定上市</h3><p>加入訂選或追蹤系列後，這裡就會整理近期上市資訊。月份未定日的書可到近期上市查看。</p><a href="#library" class="secondary button-link">去找一本想看的書 ↗</a></div>';
        return;
      }
      node("homeUpcoming").innerHTML = data.items.map(book => {
        const cover = /^https?:\/\//i.test(book.cover_url || "") ? `<img src="${escape(book.cover_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<span>${escape((book.title || "書").slice(0, 3))}</span>`;
        return `<button class="home-book" type="button" data-home-book="${Number(book.id)}"><span class="home-cover">${cover}</span><span class="home-book-info"><time datetime="${escape(book.release_date)}">${escape(book.release_date.replaceAll("-", "/"))} · 預定</time><strong>${escape(book.title)}</strong><small>${escape(book.publisher_name)} · ${escape(book.author || "作者未提供")}</small></span><span class="home-book-arrow" aria-hidden="true">↗</span></button>`;
      }).join("");
      node("homeUpcoming").querySelectorAll("[data-home-book]").forEach(button => button.addEventListener("click", () => openBook(Number(button.dataset.homeBook))));
    } catch (error) {
      if (current !== generation) return;
      node("homeUpcoming").innerHTML = '<div class="home-empty"><h3>書架摘要暫時無法載入</h3><p>你仍可使用上方的功能入口，或稍後重試。</p><button class="secondary" id="retryHome" type="button">重試摘要</button></div>';
      node("retryHome").addEventListener("click", () => load({api, user, openBook}));
    }
  }

  function reset() { generation++; }
  return {init, load, reset};
})();
