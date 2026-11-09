import { useEffect, useState } from "react";
import UploadZone from "./UploadZone.jsx";

/** Add files to the stage folder of a chosen profile. */
export default function UploadDialog({ profiles = [], defaultProfileId, types, onResults, onClose }) {
  const enabled = profiles.filter((p) => p.enabled);
  const choices = enabled.length ? enabled : profiles;
  const [profileId, setProfileId] = useState(
    () => (choices.some((p) => p.id === defaultProfileId) ? defaultProfileId : choices[0]?.id ?? null)
  );
  const profile = choices.find((p) => p.id === profileId);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="scrim scrim-center" onClick={onClose}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label="Add files" onClick={(e) => e.stopPropagation()}>
        {choices.length === 0 ? (
          <p className="empty">There are no directory profiles yet. Add one under Directories first.</p>
        ) : (
          <>
            {choices.length > 1 && (
              <p className="dialog-profile">
                <label htmlFor="upload-profile">Add to profile </label>
                <select id="upload-profile" className="input input-select" value={profileId} onChange={(e) => setProfileId(Number(e.target.value))}>
                  {choices.map((p) => (
                    <option key={p.id} value={p.id}>{p.name}{p.enabled ? "" : " (off)"}</option>
                  ))}
                </select>
              </p>
            )}
            <UploadZone key={profileId} profileId={profile.id} profileName={choices.length > 1 ? profile.name : ""} types={types} onUploaded={onResults} />
          </>
        )}
        <p className="dialog-actions">
          <button type="button" className="button button-quiet" onClick={onClose}>Done</button>
        </p>
      </div>
    </div>
  );
}
