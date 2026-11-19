function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

async function request(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    credentials: "same-origin",
    ...options,
    headers: { "X-CSRFToken": csrfToken(), ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error || `Request failed (${response.status})`);
    error.body = body; // field-level messages live in body.errors
    throw error;
  }
  return body;
}

export const getStats = () => request("/stats/");
export const getJobs = (statuses = "", limit = 50) =>
  request(`/jobs/?limit=${limit}${statuses ? `&status=${encodeURIComponent(statuses)}` : ""}`);
export const getJob = (id) => request(`/jobs/${id}/`);
export const retryJob = (id) => request(`/jobs/${id}/retry/`, { method: "POST" });
export const startEngine = () => request("/engine/start/", { method: "POST" });
export const stopEngine = (force = false) =>
  request("/engine/stop/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force }),
  });
export const getConfig = () => request("/config/");
export const saveConfig = (values) =>
  request("/config/", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
export const resetConfig = () => request("/config/reset/", { method: "POST" });

export function uploadFile(profileId, file) {
  const form = new FormData();
  form.append("file", file);
  return request(`/directories/${profileId}/upload/`, { method: "POST", body: form });
}

const sendJson = (method, path, payload) =>
  request(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload ?? {}) });

export const getDatabases = () => request("/databases/");
export const createDatabase = (payload) => sendJson("POST", "/databases/", payload);
export const updateDatabase = (id, payload) => sendJson("PUT", `/databases/${id}/`, payload);
export const deleteDatabase = (id) => request(`/databases/${id}/`, { method: "DELETE" });
export const testDatabase = (payload) => sendJson("POST", "/databases/test/", payload);

export const getDirectories = () => request("/directories/");
export const createDirectory = (payload) => sendJson("POST", "/directories/", payload);
export const updateDirectory = (id, payload) => sendJson("PUT", `/directories/${id}/`, payload);
export const deleteDirectory = (id) => request(`/directories/${id}/`, { method: "DELETE" });
export const testDirectory = (payload) => sendJson("POST", "/directories/test/", payload);
export const getDirectoryFiles = (id, folder) => request(`/directories/${id}/files/${folder}/`);

export const getMappings = () => request("/mappings/");
export const createMapping = (payload) => sendJson("POST", "/mappings/", payload);
export const updateMapping = (id, payload) => sendJson("PUT", `/mappings/${id}/`, payload);
export const deleteMapping = (id) => request(`/mappings/${id}/`, { method: "DELETE" });
export const matchNames = (rules, filenames) => sendJson("POST", "/mappings/match/", { rules, filenames });
export const getMappingTables = (connection) => sendJson("POST", "/mappings/tables/", { connection });
export const verifyMappingTables = (connection, tables) => sendJson("POST", "/mappings/verify/", { connection, tables });
