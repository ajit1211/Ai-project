import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { useAuth } from "../auth/AuthContext";

export function OrgSwitcher() {
  const { organizations, activeOrganizationId, switchOrganization } = useAuth();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  const active = organizations.find((org) => org.id === activeOrganizationId);

  useEffect(() => {
    if (!open) return;
    const onClick = (event: MouseEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const choose = (organizationId: string) => {
    setOpen(false);
    if (organizationId === activeOrganizationId) return;
    setBusy(true);
    void switchOrganization(organizationId).finally(() => {
      setBusy(false);
    });
  };

  return (
    <div className="switcher" ref={container}>
      <button
        type="button"
        className="switcher-button"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={busy}
        onClick={() => {
          setOpen((value) => !value);
        }}
      >
        <span className="switcher-name">{active?.name ?? "Select organization"}</span>
        <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <ul className="menu" role="menu">
          {organizations.map((org) => (
            <li key={org.id} role="none">
              <button
                type="button"
                role="menuitemradio"
                aria-checked={org.id === activeOrganizationId}
                className="menu-item"
                onClick={() => {
                  choose(org.id);
                }}
              >
                <span>{org.name}</span>
                <span className="muted small">
                  {org.kind === "personal" ? "Personal" : (org.role?.name ?? "")}
                </span>
              </button>
            </li>
          ))}
          <li role="separator" className="menu-separator" />
          <li role="none">
            <button
              type="button"
              role="menuitem"
              className="menu-item"
              onClick={() => {
                setOpen(false);
                void navigate("/settings/organizations?create=1");
              }}
            >
              + Create organization
            </button>
          </li>
        </ul>
      )}
    </div>
  );
}
