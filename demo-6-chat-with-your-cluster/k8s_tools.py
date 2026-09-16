"""
k8s_tools.py
Read-only Kubernetes diagnostic functions — the agent's "hands".
Uses local kubeconfig (exec-auth via aws eks get-token), no manual STS needed.
"""

from kubernetes import client, config
from kubernetes.client.rest import ApiException

# Load kubeconfig once at module import (uses current-context automatically)
config.load_kube_config()

core_v1 = client.CoreV1Api()


def get_pods(namespace: str = "default") -> list:
    """List all pods in a namespace with their status and restart count."""
    try:
        pods = core_v1.list_namespaced_pod(namespace=namespace)
        result = []
        for pod in pods.items:
            restarts = 0
            if pod.status.container_statuses:
                restarts = sum(cs.restart_count for cs in pod.status.container_statuses)
            result.append({
                "name": pod.metadata.name,
                "status": pod.status.phase,
                "restarts": restarts,
                "node": pod.spec.node_name,
            })
        return result
    except ApiException as e:
        return [{"error": f"Failed to list pods: {e.reason}"}]


def get_logs(pod_name: str, namespace: str = "default", tail_lines: int = 50) -> str:
    """Fetch recent logs from a pod. Falls back to previous container logs if pod restarted."""
    try:
        return core_v1.read_namespaced_pod_log(
            name=pod_name, namespace=namespace, tail_lines=tail_lines
        )
    except ApiException as e:
        if e.status == 400:
            # Container likely crashed — try previous logs
            try:
                return core_v1.read_namespaced_pod_log(
                    name=pod_name, namespace=namespace, tail_lines=tail_lines, previous=True
                )
            except ApiException as e2:
                return f"Failed to fetch logs (current and previous): {e2.reason}"
        return f"Failed to fetch logs: {e.reason}"


def describe_pod(pod_name: str, namespace: str = "default") -> dict:
    """Get detailed pod info: container statuses, conditions, resource limits."""
    try:
        pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        containers_info = []
        for cs in (pod.status.container_statuses or []):
            state = "unknown"
            reason = None
            if cs.state.waiting:
                state = "waiting"
                reason = cs.state.waiting.reason
            elif cs.state.terminated:
                state = "terminated"
                reason = cs.state.terminated.reason
            elif cs.state.running:
                state = "running"
            containers_info.append({
                "name": cs.name,
                "ready": cs.ready,
                "restart_count": cs.restart_count,
                "state": state,
                "reason": reason,
            })

        conditions = [
            {"type": c.type, "status": c.status, "reason": c.reason}
            for c in (pod.status.conditions or [])
        ]

        return {
            "name": pod.metadata.name,
            "phase": pod.status.phase,
            "node": pod.spec.node_name,
            "containers": containers_info,
            "conditions": conditions,
        }
    except ApiException as e:
        return {"error": f"Failed to describe pod: {e.reason}"}


def get_events(namespace: str = "default", pod_name: str = None) -> list:
    """Get recent Kubernetes events, optionally filtered to a specific pod."""
    try:
        events = core_v1.list_namespaced_event(namespace=namespace)
        result = []
        for e in events.items:
            if pod_name and e.involved_object.name != pod_name:
                continue
            result.append({
                "object": e.involved_object.name,
                "reason": e.reason,
                "message": e.message,
                "type": e.type,
                "count": e.count,
            })
        # Most recent last-occurring events are usually most relevant
        return result[-20:]
    except ApiException as e:
        return [{"error": f"Failed to fetch events: {e.reason}"}]


# Quick standalone test — run `python k8s_tools.py` to sanity-check before wiring Bedrock
if __name__ == "__main__":
    print("Testing get_pods('kube-system')...")
    pods = get_pods("kube-system")
    for p in pods:
        print(p)