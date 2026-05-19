export type ChronosMessageRole = "user" | "assistant" | "system" | "tool";

export type ChronosMessage = {
  id: string;
  conversation_id: string;
  role: ChronosMessageRole;
  content: string;
  created_at: string;
};
