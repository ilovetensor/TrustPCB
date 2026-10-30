import { Suspense } from "react";
import { Workspace } from "../../../components/workspace/Workspace";

export default async function ReviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (<Suspense fallback={null}><Workspace reviewId={id} /></Suspense>);
}
