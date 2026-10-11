/* Positive catalog labels only. Unknown is neither proven BL nor proven R18. */
(() => {
  const options = Object.freeze([
    ["general", "一般向"], ["bl", "BL"], ["r18", "R18"],
  ]);
  function parse(value) {
    if (value === "" || value === "none") return [];
    const parts = typeof value === "string" ? value.split(",") : [];
    if (!parts.length || parts.length > 3 || new Set(parts).size !== parts.length ||
        parts.some(key => !["general", "bl", "r18", "bl_r18"].includes(key))) throw new Error("無效的內容分類篩選");
    return parts;
  }
  function allowed(mode, value) {
    try { parse(value); } catch { return false; }
    if (value === "" || value === "general") return true;
    if (value === "none") return true;
    if (value.includes(",")) return parse(value).every(key => allowed(mode, key));
    if (value === "bl_r18") return mode === "all";
    return mode === "all" || mode === value;
  }
  function matches(book, value) {
    const bl = Boolean(book.bl_category), r18 = book.content_rating === "restricted_18";
    const parts = parse(value);
    return value === "" || parts.some(key => (key === "general" && !bl && !r18) ||
      (key === "bl" && bl) || (key === "r18" && r18) || (key === "bl_r18" && bl && r18));
  }
  function choices(mode) { return options.filter(([key]) => allowed(mode, key)); }
  function selection(value, mode) {
    const available = choices(mode).map(([key]) => key);
    if (value === "") return available;
    if (value === "none") return [];
    return parse(value).filter(key => available.includes(key));
  }
  function encode(selected, mode) {
    const available = choices(mode).map(([key]) => key);
    const keys = available.filter(key => selected.includes(key));
    return keys.length === available.length ? "" : keys.join(",") || "none";
  }
  function render(input, fieldset, summary, mode) {
    // Preference changes discard disallowed selections; no hidden restrictive filter.
    const retained = selection(input.value, mode);
    input.value = input.value !== "none" && !retained.length ? "" : encode(retained, mode);
    const selected = selection(input.value, mode);
    fieldset.innerHTML = '<legend class="sr-only">選擇內容分類</legend>' + choices(mode).map(([key, label]) =>
      `<label><input type="checkbox" value="${key}" ${selected.includes(key) ? "checked" : ""}>${label}</label>`).join("");
    summary.textContent = !selected.length ? "未選分類" : selected.length === choices(mode).length && selected.length > 1 ? "全部分類" :
      options.filter(([key]) => selected.includes(key)).map(([, label]) => label).join("、");
  }
  window.AniShelfContentFilters = {options, allowed, matches, render, choices, selection, encode};
})();
