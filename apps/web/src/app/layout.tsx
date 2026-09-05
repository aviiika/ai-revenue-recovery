import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Revenue Recovery Agent",
  description:
    "Detects revenue at risk, diagnoses it, selects a bounded recovery intervention, and measures money recovered.",
};

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/cases", label: "Recovery cases" },
  { href: "/reviews", label: "Review queue" },
  { href: "/model", label: "Model metrics" },
  { href: "/experiments", label: "Incremental impact" },
  { href: "/policies", label: "Policies" },
];

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <Providers>
          {/* Desktop-first operations shell: persistent left rail, dense
              content column. Collapses to a horizontal bar on narrow screens
              rather than hiding navigation behind a menu, because operators
              switch views constantly. */}
          <div className="flex min-h-screen flex-col md:flex-row">
            <nav
              aria-label="Primary"
              className="shrink-0 border-b border-[var(--color-border)] bg-[var(--color-surface)] md:w-56 md:border-r md:border-b-0"
            >
              <div className="px-4 py-4">
                <Link href="/" className="block">
                  <div className="text-sm font-semibold tracking-tight text-[var(--color-ink)]">
                    Revenue Recovery
                  </div>
                  <div className="text-2xs text-[var(--color-ink-muted)]">
                    Agent console
                  </div>
                </Link>
              </div>
              <ul className="flex gap-1 px-2 pb-2 md:flex-col md:gap-0.5">
                {NAV.map((item) => (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      className="block rounded px-2.5 py-1.5 text-sm text-[var(--color-ink-secondary)] hover:bg-[var(--color-surface-sunken)] hover:text-[var(--color-ink)]"
                    >
                      {item.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </nav>

            <main className="min-w-0 flex-1 px-4 py-6 md:px-8">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
