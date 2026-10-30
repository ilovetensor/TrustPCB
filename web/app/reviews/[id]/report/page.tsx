import { Report } from "../../../../components/report/Report";
export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <Report reviewId={id} />;
}
