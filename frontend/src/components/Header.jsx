import Logo from "./Logo.jsx";

export default function Header({ onAddFiles, onRefresh }) {
  return (
    <header className="header">
      <div className="brand">
        <Logo />
        <span className="brand-name">Dataloader</span>
      </div>
      <div className="header-actions">
        <button type="button" className="hbtn hbtn-primary" onClick={onAddFiles}>Add files</button>
        <button type="button" className="hbtn" onClick={onRefresh}>Refresh</button>
        <a className="hbtn" href="/admin/" target="_blank" rel="noreferrer">Admin</a>
      </div>
    </header>
  );
}
