import { RfLab } from "../../../../components/rf/RfLab";
export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <RfLab reviewId={id} />;
}
