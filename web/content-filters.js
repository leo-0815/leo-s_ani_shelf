/* Positive catalog labels only. Unknown is neither proven BL nor proven R18. */
(() => {
  const options = Object.freeze([
    ["", "所有可見內容"], ["general", "一般向（非 BL／R18）"],
    ["bl", "BL"], ["r18", "R18"], ["bl_r18", "BL 且 R18"],
  ]);
  function allowed(mode, value) {
    if (!options.some(([key]) => key === value)) return false;
    if (value === "" || value === "general") return true;
    if (value === "bl_r18") return mode === "all";
    return mode === "all" || mode === value;
  }
  function matches(book, value) {
    const bl = Boolean(book.bl_category), r18 = book.content_rating === "restricted_18";
    if (!options.some(([key]) => key === value)) throw new Error("無效的內容分類篩選");
    return value === "" || (value === "general" && !bl && !r18) ||
      (value === "bl" && bl) || (value === "r18" && r18) || (value === "bl_r18" && bl && r18);
  }
  function render(select, mode) {
    const current = select.value;
    select.innerHTML = options.map(([value, label]) =>
      `<option value="${value}" ${allowed(mode, value) ? "" : "disabled"}>${label}${allowed(mode, value) ? "" : "（偏好未開啟）"}</option>`).join("");
    select.value = allowed(mode, current) ? current : "";
  }
  window.AniShelfContentFilters = {options, allowed, matches, render};
})();
