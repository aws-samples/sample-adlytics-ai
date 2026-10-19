// SPDX-License-Identifier: MIT-0

/** The gradient brand mark shared by the navigation, the menu sheet, the assistant head and the auth states. */
export function BrandMark({ className, gradientId }: { readonly className: string; readonly gradientId: string }) {
  return (
    <svg className={className} viewBox="0 0 34 34" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id={gradientId} x1="0" y1="1" x2="1" y2="0">
          <stop offset="0" stopColor="#22d3ee" />
          <stop offset="1" stopColor="#8b5cf6" />
        </linearGradient>
      </defs>
      <rect x="1" y="1" width="32" height="32" rx="9" fill={`url(#${gradientId})`} />
      <path d="M19.5 6 10 19h5.4l-1.6 9L24 15h-5.6l1.1-9z" fill="#fff" opacity="0.96" />
      <circle cx="25.5" cy="9" r="2" fill="#fff" opacity="0.85" />
    </svg>
  );
}
