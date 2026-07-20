import { redirect } from "next/navigation";

export default function TasksPage() {
  redirect("/activity?tab=tasks");
}
