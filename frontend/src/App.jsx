import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getDatabases, getDirectories, getJobs, getMappings, getStats } from "./api.js";
import Footer from "./components/Footer.jsx";
import Header from "./components/Header.jsx";
import JobDetail from "./components/JobDetail.jsx";
import NavTree from "./components/NavTree.jsx";
import UploadDialog from "./components/UploadDialog.jsx";
import { usePolling } from "./hooks.js";
import { buildTree, findNode } from "./tree.js";
import ConfigView from "./views/ConfigView.jsx";
import DatabaseList from "./views/DatabaseList.jsx";
import DatabaseView from "./views/DatabaseView.jsx";
import MappingList from "./views/MappingList.jsx";
import MappingView from "./views/MappingView.jsx";
import DirectoryList from "./views/DirectoryList.jsx";
import DirectoryProfileView from "./views/DirectoryProfileView.jsx";
import JobsView from "./views/JobsView.jsx";
import Overview from "./views/Overview.jsx";

const POLL_MS = 3000;
const STATUS_FILTER = {
  "jobs:all": "",
  "jobs:loaded": "loaded",
  "jobs:failed": "failed",
  "jobs:progress": "queued,processing,requeued",
};
const shorten = (text, n = 140) => (text.length > n ? `${text.slice(0, n)}…` : text);

export default function App() {
  const [stats, setStats] = useState(null);
  const [recent, setRecent] = useState([]);
  const [viewJobs, setViewJobs] = useState([]);
  const [selected, setSelected] = useState("overview");
  const [detailId, setDetailId] = useState(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [refreshKey, setRefreshKey] = useState(0);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [serverDown, setServerDown] = useState(false);
  const [databases, setDatabases] = useState({ connections: [], kinds: [], loaded: false });
  const [profiles, setProfiles] = useState({ list: [], loaded: false });
  const [mappings, setMappings] = useState({ list: [], loaded: false });

  const nextId = useRef(0);
  const seenJobs = useRef(null);
  const wasOnline = useRef(null);
  const wasDown = useRef(false);
  const dirtyRef = useRef(false); // a form with unsaved edits is open

  const notify = useCallback((text, tone = "info") => {
    setMessages((prev) => [...prev, { id: nextId.current++, time: new Date(), tone, text }].slice(-50));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const wantsJobs = selected in STATUS_FILTER;
      const [s, r, v] = await Promise.all([
        getStats(),
        getJobs("", 20),
        wantsJobs ? getJobs(STATUS_FILTER[selected], 200) : Promise.resolve(null),
      ]);

      if (wasDown.current) notify("Reconnected to the server.", "ok");
      wasDown.current = false;
      setServerDown(false);

      if (wasOnline.current !== null && wasOnline.current !== s.engine_online) {
        notify(s.engine_online ? "Engine started." : "Engine stopped.", s.engine_online ? "ok" : "bad");
      }
      wasOnline.current = s.engine_online;

      if (seenJobs.current) {
        for (const job of r.jobs) {
          if (seenJobs.current[job.id] === job.status) continue;
          if (job.status === "loaded") notify(`${job.filename} loaded: ${job.rows_loaded.toLocaleString()} rows.`, "ok");
          if (job.status === "failed") notify(`${job.filename} failed. ${shorten(job.error)}`, "bad");
        }
      }
      seenJobs.current = Object.fromEntries(r.jobs.map((j) => [j.id, j.status]));

      setStats(s);
      setProfiles({ list: s.profiles, loaded: true }); // counts stay live; the list also feeds the tree
      setRecent(r.jobs);
      if (v) setViewJobs(v.jobs);
      setUpdatedAt(new Date());
    } catch (e) {
      if (!wasDown.current) notify(`Can’t reach the server: ${e.message}`, "bad");
      wasDown.current = true;
      setServerDown(true);
    }
  }, [selected, notify]);

  usePolling(refresh, POLL_MS, [selected]);
  useEffect(() => setViewJobs([]), [selected]);

  const setDirty = useCallback((dirty) => {
    dirtyRef.current = dirty;
  }, []);

  const handleSelect = useCallback((id) => {
    if (dirtyRef.current && !window.confirm("You have unsaved changes. Discard them?")) return;
    dirtyRef.current = false;
    setSelected(id);
  }, []);

  const loadDatabases = useCallback(async () => {
    try {
      const d = await getDatabases();
      setDatabases({ connections: d.connections, kinds: d.kinds, loaded: true });
    } catch (e) {
      notify(`Couldn’t load database connections: ${e.message}`, "bad");
    }
  }, [notify]);

  useEffect(() => {
    loadDatabases();
  }, [loadDatabases]);

  const loadProfiles = useCallback(async () => {
    try {
      const d = await getDirectories();
      setProfiles({ list: d.profiles, loaded: true });
    } catch (e) {
      notify(`Couldn’t load directory profiles: ${e.message}`, "bad");
    }
  }, [notify]);

  useEffect(() => {
    loadProfiles();
  }, [loadProfiles]);

  const loadMappings = useCallback(async () => {
    try {
      const d = await getMappings();
      setMappings({ list: d.mappings, loaded: true });
    } catch (e) {
      notify(`Couldn’t load mappings: ${e.message}`, "bad");
    }
  }, [notify]);

  useEffect(() => {
    loadMappings();
  }, [loadMappings]);

  // Profiles and connections show up inside mappings, so reload both after changing either.
  const reloadProfiles = useCallback(async () => {
    await loadProfiles();
    await loadMappings();
  }, [loadProfiles, loadMappings]);
  const reloadDatabases = useCallback(async () => {
    await loadDatabases();
    await loadMappings();
  }, [loadDatabases, loadMappings]);

  // The tree only changes when a profile is added, renamed, switched on/off or removed, not on every poll.
  const profileSig = JSON.stringify(profiles.list.map((p) => [p.id, p.name, p.enabled]));
  const mappingSig = JSON.stringify(mappings.list.map((m) => [m.id, m.profile_name, m.connection_name, m.enabled]));
  const tree = useMemo(
    () => buildTree(databases.connections, databases.kinds, profiles.list, mappings.list),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [databases, profileSig, mappingSig]
  );

  const manualRefresh = () => {
    setRefreshKey((k) => k + 1);
    refresh();
  };

  const onUploaded = useCallback(
    (results = []) => {
      results.forEach((r) => notify(r.text, r.ok ? "ok" : "bad"));
      refresh();
    },
    [notify, refresh]
  );

  const counts = useMemo(() => {
    const own = {
      ...(databases.loaded ? { databases: databases.connections.length } : {}),
      ...(mappings.loaded ? { mappings: mappings.list.length } : {}),
    };
    if (!stats) return own;
    const j = stats.jobs;
    return {
      ...own,
      directories: stats.profiles.length,
      ...Object.fromEntries(stats.profiles.map((p) => [`prof:${p.id}`, p.counts.stage])),
      "jobs:all": Object.values(j).reduce((a, b) => a + b, 0),
      "jobs:loaded": j.loaded ?? 0,
      "jobs:failed": j.failed ?? 0,
      "jobs:progress": (j.queued ?? 0) + (j.processing ?? 0) + (j.requeued ?? 0),
    };
  }, [stats, databases, mappings]);

  const node = findNode(selected, tree);

  let view;
  if (selected === "overview") {
    view = <Overview stats={stats} jobs={recent} onSelectJob={setDetailId} onGo={handleSelect} onEngineChanged={refresh} notify={notify} />;
  } else if (selected === "directories") {
    view = (
      <DirectoryList
        profiles={profiles}
        notify={notify}
        onAdd={() => handleSelect("prof:new")}
        onOpen={(id) => handleSelect(`prof:${id}`)}
        onReload={reloadProfiles}
      />
    );
  } else if (selected.startsWith("prof:")) {
    view = (
      <DirectoryProfileView
        key={selected}
        id={selected.slice(5)}
        profiles={profiles}
        types={stats?.supported_types}
        refreshKey={refreshKey}
        notify={notify}
        onDirtyChange={setDirty}
        onBack={() => handleSelect("directories")}
        onUploaded={onUploaded}
        onSaved={async (saved) => {
          await reloadProfiles();
          setSelected(`prof:${saved.id}`);
        }}
        onDeleted={async () => {
          await reloadProfiles();
          setSelected("directories");
        }}
      />
    );
  } else if (selected === "databases") {
    view = (
      <DatabaseList
        databases={databases}
        notify={notify}
        onAdd={() => handleSelect("db:new")}
        onOpen={(id) => handleSelect(`db:${id}`)}
        onReload={reloadDatabases}
      />
    );
  } else if (selected.startsWith("db:")) {
    view = (
      <DatabaseView
        key={selected}
        id={selected.slice(3)}
        databases={databases}
        notify={notify}
        onDirtyChange={setDirty}
        onReload={loadDatabases}
        onBack={() => handleSelect("databases")}
        onSaved={async (saved) => {
          await reloadDatabases();
          setSelected(`db:${saved.id}`);
        }}
        onDeleted={async () => {
          await reloadDatabases();
          setSelected("databases");
        }}
      />
    );
  } else if (selected === "mappings") {
    view = (
      <MappingList
        mappings={mappings}
        profiles={profiles}
        databases={databases}
        notify={notify}
        onAdd={() => handleSelect("map:new")}
        onOpen={(id) => handleSelect(`map:${id}`)}
        onReload={loadMappings}
      />
    );
  } else if (selected.startsWith("map:")) {
    view = (
      <MappingView
        key={selected}
        id={selected.slice(4)}
        mappings={mappings}
        profiles={profiles}
        databases={databases}
        notify={notify}
        onDirtyChange={setDirty}
        onBack={() => handleSelect("mappings")}
        onSaved={async (saved) => {
          await loadMappings();
          setSelected(`map:${saved.id}`);
        }}
        onDeleted={async () => {
          await loadMappings();
          setSelected("mappings");
        }}
      />
    );
  } else if (selected in STATUS_FILTER) {
    view = <JobsView jobs={viewJobs} onSelect={setDetailId} />;
  } else {
    view = <ConfigView notify={notify} onDirtyChange={setDirty} />;
  }

  return (
    <div className="shell">
      <Header onAddFiles={() => setUploadOpen(true)} onRefresh={manualRefresh} />
      <NavTree nodes={tree} selectedId={selected} onSelect={handleSelect} counts={counts} />
      <main className="main" id="main">
        <div className="main-inner">
          <div className="main-head">
            <h1>{node?.title ?? "Database"}</h1>
            <p>{node?.description ?? ""}</p>
          </div>
          {view}
        </div>
      </main>
      <Footer
        stats={stats}
        serverDown={serverDown}
        messages={messages}
        updatedAt={updatedAt}
        onClear={() => setMessages([])}
      />
      {uploadOpen && (
        <UploadDialog
          profiles={profiles.list}
          defaultProfileId={selected.startsWith("prof:") ? Number(selected.slice(5)) || undefined : undefined}
          types={stats?.supported_types} onResults={onUploaded} onClose={() => setUploadOpen(false)} />
      )}
      {detailId != null && <JobDetail id={detailId} onClose={() => setDetailId(null)} onChanged={refresh} />}
    </div>
  );
}
