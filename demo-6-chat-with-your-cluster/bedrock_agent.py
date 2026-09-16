"""
bedrock_agent.py
The agent's "brain" — uses Bedrock Converse API with tool-use to reason
about your question, call K8s tool functions, and explain the diagnosis.
"""

import boto3
import json
from k8s_tools import get_pods, get_logs, describe_pod, get_events

# Bedrock client stays hardcoded to us-east-1 (cross-region inference profile),
# even though the EKS cluster is in eu-central-1.
bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")

MODEL_ID = "global.anthropic.claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a Kubernetes troubleshooting assistant with read-only access to a live EKS cluster.

Your job: help the user diagnose issues by calling the available tools, reasoning over the results, and explaining the root cause in clear, plain English.

Guidelines:
- Always start by checking pod status if the user asks about a problem (use get_pods).
- If a pod shows unusual restarts or a non-Running status, dig deeper with describe_pod and get_logs.
- Check get_events for cluster-level context (e.g. scheduling failures, image pull errors) when pod-level info isn't enough.
- Chain multiple tool calls if needed — don't stop after one call if the picture isn't complete.
- When you give your final answer, clearly state: (1) what's wrong, (2) the likely root cause, (3) a suggested fix.
- Keep explanations concise and readable — this will be shown live to an audience.
"""

# Tool definitions — tells Bedrock what functions are available and their parameters
TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": "get_pods",
                "description": "List all pods in a namespace with status and restart count. Use this first to get an overview.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "namespace": {
                                "type": "string",
                                "description": "Kubernetes namespace to check (default: 'default')"
                            }
                        }
                    }
                }
            }
        },
        {
            "toolSpec": {
                "name": "get_logs",
                "description": "Fetch recent logs from a specific pod. Automatically falls back to previous container logs if the pod crashed.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "pod_name": {"type": "string", "description": "Exact pod name"},
                            "namespace": {"type": "string", "description": "Namespace (default: 'default')"},
                            "tail_lines": {"type": "integer", "description": "Number of recent log lines to fetch (default: 50)"}
                        },
                        "required": ["pod_name"]
                    }
                }
            }
        },
        {
            "toolSpec": {
                "name": "describe_pod",
                "description": "Get detailed pod info: container states, restart reasons, conditions. Use when a pod isn't Running or is restarting.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "pod_name": {"type": "string", "description": "Exact pod name"},
                            "namespace": {"type": "string", "description": "Namespace (default: 'default')"}
                        },
                        "required": ["pod_name"]
                    }
                }
            }
        },
        {
            "toolSpec": {
                "name": "get_events",
                "description": "Get recent Kubernetes events for cluster-level context (e.g. scheduling failures, image pull errors, OOM kills).",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "namespace": {"type": "string", "description": "Namespace (default: 'default')"},
                            "pod_name": {"type": "string", "description": "Optional: filter events to a specific pod"}
                        }
                    }
                }
            }
        }
    ]
}

# Map tool names to actual Python functions
TOOL_FUNCTIONS = {
    "get_pods": get_pods,
    "get_logs": get_logs,
    "describe_pod": describe_pod,
    "get_events": get_events,
}


def execute_tool(tool_name: str, tool_input: dict):
    """Run the requested tool function with the arguments Bedrock provided."""
    func = TOOL_FUNCTIONS.get(tool_name)
    if not func:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        return func(**tool_input)
    except Exception as e:
        return {"error": f"Tool execution failed: {str(e)}"}


def ask_agent(user_question: str, max_iterations: int = 6) -> str:
    """
    Main agent loop:
    1. Send question to Bedrock with tool definitions
    2. If Bedrock wants to call a tool, execute it and feed the result back
    3. Repeat until Bedrock gives a final text answer (or max_iterations hit)
    """
    messages = [
        {"role": "user", "content": [{"text": user_question}]}
    ]

    for iteration in range(max_iterations):
        response = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=messages,
            toolConfig=TOOL_CONFIG,
        )

        output_message = response["output"]["message"]
        messages.append(output_message)

        stop_reason = response["stopReason"]

        if stop_reason == "tool_use":
            tool_results = []
            for content_block in output_message["content"]:
                if "toolUse" in content_block:
                    tool_use = content_block["toolUse"]
                    tool_name = tool_use["name"]
                    tool_input = tool_use.get("input", {})
                    tool_use_id = tool_use["toolUseId"]

                    print(f"  [agent action] calling {tool_name}({tool_input})")
                    result = execute_tool(tool_name, tool_input)

                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [{"json": {"result": result}}]
                        }
                    })

            messages.append({"role": "user", "content": tool_results})
            continue

        else:
            # Final answer — extract text
            final_text = ""
            for content_block in output_message["content"]:
                if "text" in content_block:
                    final_text += content_block["text"]
            return final_text

    return "Agent stopped: too many reasoning steps without a final answer."

def ask_agent_stream(user_question: str, max_iterations: int = 6):
    """
    Generator version of ask_agent — yields step-by-step events for live UI streaming.
    Each yielded item is a dict: {"type": "tool_call"|"tool_result"|"final", ...}
    """
    messages = [
        {"role": "user", "content": [{"text": user_question}]}
    ]

    for iteration in range(max_iterations):
        response = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=messages,
            toolConfig=TOOL_CONFIG,
        )

        output_message = response["output"]["message"]
        messages.append(output_message)

        stop_reason = response["stopReason"]

        if stop_reason == "tool_use":
            tool_results = []
            for content_block in output_message["content"]:
                if "toolUse" in content_block:
                    tool_use = content_block["toolUse"]
                    tool_name = tool_use["name"]
                    tool_input = tool_use.get("input", {})
                    tool_use_id = tool_use["toolUseId"]

                    yield {"type": "tool_call", "tool": tool_name, "input": tool_input}

                    result = execute_tool(tool_name, tool_input)

                    yield {"type": "tool_result", "tool": tool_name, "result": result}

                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [{"json": {"result": result}}]
                        }
                    })

            messages.append({"role": "user", "content": tool_results})
            continue

        else:
            final_text = ""
            for content_block in output_message["content"]:
                if "text" in content_block:
                    final_text += content_block["text"]
            yield {"type": "final", "text": final_text}
            return

    yield {"type": "final", "text": "Agent stopped: too many reasoning steps without a final answer."}


# Quick standalone test
if __name__ == "__main__":
    question = input("Ask the agent about your cluster: ")
    print("\nThinking...\n")
    answer = ask_agent(question)
    print("=" * 60)
    print(answer)