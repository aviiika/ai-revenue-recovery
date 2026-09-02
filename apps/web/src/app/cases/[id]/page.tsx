import { CaseDetailView } from "./case-detail-view";

/**
 * Next.js 16 breaking change: `params` is a Promise and must be awaited.
 * Synchronous access was removed after the v15 compatibility period.
 */
export default async function CaseDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <CaseDetailView caseId={id} />;
}
