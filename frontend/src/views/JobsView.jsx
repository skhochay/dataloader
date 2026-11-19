import JobsTable from "../components/JobsTable.jsx";

export default function JobsView({ jobs, onSelect }) {
  return <JobsTable jobs={jobs} onSelect={onSelect} />;
}
