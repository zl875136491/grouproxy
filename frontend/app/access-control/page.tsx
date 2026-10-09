import { redirect } from "next/navigation";

export default function AccessControlIndexPage() {
  redirect("/access-control/groups");
}
