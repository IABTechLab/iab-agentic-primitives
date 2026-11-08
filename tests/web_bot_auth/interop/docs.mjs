/**
 * The message documents at the harness's boundary.
 *
 * The vector files and the adapter protocol write a message's field lines as
 * [name, value] pairs, in order, and its body as text, or as {"base64": ...}
 * when the bytes aren't UTF-8. Inside the harness a message has a header
 * object and a text body. fromWire reads the first form into the second, and
 * toWire writes it back. Only a "headers" or "body" member that is a document
 * part changes; a scenario's after_signing edits stay as written.
 */

const isPlain = v => v !== null && typeof v === 'object' && !Array.isArray(v);

/** The header object of [name, value] pairs; a repeated name's values join with ", ". */
function headerObject(pairs) {
  const out = {};
  for (const [name, value] of pairs) out[name] = name in out ? `${out[name]}, ${value}` : value;
  return out;
}

export function fromWire(value) {
  if (Array.isArray(value)) return value.map(fromWire);
  if (!isPlain(value)) return value;
  return Object.fromEntries(Object.entries(value).map(([k, v]) => {
    if (k === 'headers' && Array.isArray(v)) return [k, headerObject(v)];
    if (k === 'body' && isPlain(v) && typeof v.base64 === 'string') return [k, Buffer.from(v.base64, 'base64').toString('utf8')];
    return [k, fromWire(v)];
  }));
}

export function toWire(value) {
  if (Array.isArray(value)) return value.map(toWire);
  if (!isPlain(value)) return value;
  return Object.fromEntries(Object.entries(value).map(([k, v]) =>
    k === 'headers' && isPlain(v) ? [k, Object.entries(v)] : [k, toWire(v)]));
}
