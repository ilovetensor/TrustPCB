import { SimLab } from "../../../../components/simlab/SimLabPage";
export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <SimLab reviewId={id} />;
}
