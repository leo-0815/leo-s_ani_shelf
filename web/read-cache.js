/* Session memory only: never persist account responses or share them with another tab. */
window.AniShelfReadCache = (() => {
  const copy = value => JSON.parse(JSON.stringify(value));
  function group(path) {
    return /^\/api\/(books|series|upcoming|recommendations|home|stats|publishers|quality|release-checks|resource-guards|preferences|notification-preferences)(?:\/|\?|$)/.exec(path)?.[1] || "";
  }
  function canonical(path) {
    const [name, query = ""] = path.split("?");
    const params = new URLSearchParams(query);
    params.sort();
    return name + (params.size || params.toString() ? `?${params}` : "");
  }
  function create({now = () => Date.now(), maxEntries = 30, maxBytes = 8 * 1024 * 1024} = {}) {
    const entries = new Map();
    let bytes = 0, generation = 0;
    function remove(key) {const entry = entries.get(key); if (entry) {bytes -= entry.bytes; entries.delete(key);}}
    return {
      get generation() {return generation;},
      key(scope, path) {return JSON.stringify([scope, canonical(path)]);},
      get(scope, path) {
        const key = this.key(scope, path), entry = entries.get(key);
        if (!entry) return undefined;
        if (entry.until <= now()) {remove(key); return undefined;}
        entries.delete(key); entries.set(key, entry);
        return copy(entry.value);
      },
      set(scope, path, value) {
        const category = group(path);
        if (!category) return;
        const serialized = JSON.stringify(value), size = serialized.length * 2;
        if (size > maxBytes) return;
        const key = this.key(scope, path);
        remove(key);
        entries.set(key, {value:JSON.parse(serialized), group:category, bytes:size,
          until:now() + (category === "publishers" ? 15 : 5) * 60000});
        bytes += size;
        while (entries.size > maxEntries || bytes > maxBytes) remove(entries.keys().next().value);
      },
      invalidate(groups) {
        generation++;
        for (const [key, entry] of entries) if (!groups || groups.includes(entry.group)) remove(key);
      },
      clear() {this.invalidate();},
    };
  }
  return {create, group, canonical};
})();
