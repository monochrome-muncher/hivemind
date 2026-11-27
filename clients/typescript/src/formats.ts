export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export interface AnthropicTool {
  name: string;
  description: string;
  input_schema: { type: "object"; [key: string]: unknown };
}

export interface OpenAITool {
  type: "function";
  function: { name: string; description: string; parameters: Record<string, unknown> };
}

export interface OpenAIResponsesTool {
  type: "function";
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

export function toAnthropic(tools: readonly ToolDefinition[]): AnthropicTool[] {
  return tools.map((t) => ({ name: t.name, description: t.description, input_schema: { ...t.inputSchema, type: "object" } }));
}

export function toOpenAI(tools: readonly ToolDefinition[]): OpenAITool[] {
  return tools.map((t) => ({
    type: "function",
    function: { name: t.name, description: t.description, parameters: t.inputSchema },
  }));
}

export function toOpenAIResponses(tools: readonly ToolDefinition[]): OpenAIResponsesTool[] {
  return tools.map((t) => ({ type: "function", name: t.name, description: t.description, parameters: t.inputSchema }));
}
