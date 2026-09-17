import React from 'react';

// Same identity sprite the sidebar/login/apply pages already use.
const ICONS = '/platform-ui/icons.svg';

// Deliberately minimal: a title (in the existing page-title weight/size, now
// token-driven) with a thin --platform-gradient accent underneath — the
// smallest possible extension of the identity into page content, scoped to
// exactly this and nothing else. `icon` is opt-in for pages where a leading
// icon reads naturally; most won't need it. `style` passes through to the
// root element only for the couple of pages whose original <h1> carried its
// own spacing (e.g. marginBottom) — never for anything beyond that.
export default function PageHeader({ title, icon, style }) {
    return (
        <div className="page-header" style={style}>
            {icon && (
                <span className="page-header-icon" aria-hidden="true">
                    <svg className="page-header-icon-svg">
                        <use href={`${ICONS}#${icon}`} />
                    </svg>
                </span>
            )}
            <h1 className="page-header-title">{title}</h1>
        </div>
    );
}
