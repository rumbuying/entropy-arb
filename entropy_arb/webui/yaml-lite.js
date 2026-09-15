/* Minimal YAML subset for engine configs: nested maps (2-space indent),
   scalars (numbers / bools / strings / null), line comments.
   The server re-validates everything through the real PyYAML + load_config,
   so this parser only has to be right for the shapes the engine uses. */

export function parseYaml(text) {
  const root = {};
  const stack = [{ indent: -1, obj: root }];
  const lines = (text || "").split(/\r?\n/);
  for (const raw of lines) {
    if (!raw.trim() || raw.trim().startsWith("#")) continue;
    const indent = raw.length - raw.trimStart().length;
    const line = raw.trim();
    const m = line.match(/^([^:#]+):\s*(.*)$/);
    if (!m) continue;
    const key = m[1].trim().replace(/^["']|["']$/g, "");
    const valText = m[2].trim();
    while (stack.length > 1 && indent <= stack[stack.length - 1].indent) {
      stack.pop();
    }
    const parent = stack[stack.length - 1].obj;
    if (valText === "") {
      const child = {};
      parent[key] = child;
      stack.push({ indent, obj: child });
    } else {
      parent[key] = parseScalar(valText);
    }
  }
  return root;
}

function parseScalar(t) {
  // strip trailing comment outside quotes
  let out = "", q = null;
  for (let i = 0; i < t.length; i++) {
    const c = t[i];
    if (q) { out += c; if (c === q) q = null; continue; }
    if (c === "#" && out.trim() === "") break;
    if (c === "#") break;
    if (c === '"' || c === "'") { q = c; continue; }
    out += c;
  }
  const v = out.trim();
  if (v === "" || v === "~" || v === "null") return null;
  if (v === "true" || v === "True") return true;
  if (v === "false" || v === "False") return false;
  if (/^-?\d+$/.test(v)) return parseInt(v, 10);
  if (/^-?\d*\.\d+$/.test(v)) return parseFloat(v);
  return v;
}

export function emitYaml(obj, indent = 0) {
  const pad = "  ".repeat(indent);
  const lines = [];
  for (const [k, v] of Object.entries(obj)) {
    if (v === null || v === undefined) { lines.push(`${pad}${k}:`); continue; }
    if (typeof v === "object") {
      lines.push(`${pad}${k}:`);
      lines.push(emitYaml(v, indent + 1));
    } else {
      lines.push(`${pad}${k}: ${formatScalar(v)}`);
    }
  }
  return lines.join("\n");
}

function formatScalar(v) {
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return String(v);
  const s = String(v);
  if (/[#'"\n:]/.test(s) || s.trim() !== s || s === "") return `'${s}'`;
  return s;
}

/* Merge a flat {path: value} map into a parsed config object. */
export function setPaths(obj, flat) {
  for (const [path, value] of Object.entries(flat)) {
    const parts = path.split(".");
    let cur = obj;
    for (let i = 0; i < parts.length - 1; i++) {
      if (typeof cur[parts[i]] !== "object" || cur[parts[i]] === null) {
        cur[parts[i]] = {};
      }
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = value;
  }
  return obj;
}
