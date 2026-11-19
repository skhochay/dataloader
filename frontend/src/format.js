const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });

export const formatDate = (iso) => (iso ? dateTime.format(new Date(iso)) : "–");

export function formatDuration(startIso, endIso) {
  if (!endIso) return "–";
  const ms = new Date(endIso) - new Date(startIso);
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export const STATUS_LABEL = {
  queued: "Queued",
  processing: "Loading",
  loaded: "Loaded",
  failed: "Failed",
  requeued: "Queued for retry",
};

const time = new Intl.DateTimeFormat(undefined, { timeStyle: "medium" });
export const formatTime = (value) => time.format(value instanceof Date ? value : new Date(value));
