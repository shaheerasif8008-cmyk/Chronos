import { redirect } from "next/navigation";

export default function ComputerPage() {
  redirect("/activity?tab=computer");
}
